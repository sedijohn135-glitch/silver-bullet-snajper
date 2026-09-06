"""Trade lifecycle monitoring: fills, closures, and P&L attribution."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")

import pytest

from config import load_config
from engine.monitor import TradeMonitor
from engine.state import BotState
from mcp_client.ctrader import CTraderGateway
from tests.fake_mcp import FakeConnection, MONEY_SCALE, _p
from utils.telegram import NullNotifier

DAY = datetime(2026, 9, 5, tzinfo=timezone.utc)


def position_record(position_id: int, *, label: str = "", entry: float = 79985.18):
    return {
        "positionId": position_id, "symbolId": 10026, "symbolName": "BTCUSD",
        "tradeSide": "BUY", "volume": 9, "entryPrice": _p(entry),
        "stopLoss": _p(79927.56), "takeProfit": _p(80154.81), "label": label,
    }


def deal_record(position_id: int, profit: float):
    return {
        "dealId": 1, "positionId": position_id, "symbolId": 10026, "volume": 9,
        "executionPrice": _p(79927.56), "netProfit": int(profit * MONEY_SCALE),
        "executionTimestamp": int((DAY + timedelta(hours=19)).timestamp() * 1000),
    }


async def build(connection: FakeConnection):
    cfg = load_config()
    gateway = CTraderGateway(cfg, connection)
    await gateway.bootstrap()
    gateway.set_active("BTCUSD")
    state = BotState()
    return TradeMonitor(cfg, gateway, NullNotifier(), state), state


@pytest.mark.asyncio
async def test_a_fill_is_reported_even_when_the_broker_drops_our_label():
    """Brokers do not reliably echo an order's label onto the position."""
    connection = FakeConnection()
    monitor, _ = await build(connection)
    await monitor.poll(DAY)          # first poll syncs whatever already existed

    connection.positions = [position_record(669454576, label="")]   # no label
    report = await monitor.poll(DAY)

    assert [p.position_id for p in report.filled] == ["669454576"]


@pytest.mark.asyncio
async def test_a_closure_with_no_deal_yet_is_deferred_not_reported_as_zero():
    """The closing deal lands in get_deals a moment after the position vanishes."""
    connection = FakeConnection()
    monitor, _ = await build(connection)

    await monitor.poll(DAY)                     # sync poll
    connection.positions = [position_record(669454576)]
    await monitor.poll(DAY)                     # position is now tracked

    connection.positions = []                   # it closed; no deal published yet
    report = await monitor.poll(DAY)
    assert report.closed == [], "must not announce +0.00 before the deal arrives"

    connection.deals = [deal_record(669454576, -5.19)]
    report = await monitor.poll(DAY)
    assert len(report.closed) == 1
    closure = report.closed[0]
    assert closure.pnl_known is True
    assert closure.pnl == pytest.approx(-5.19)
    assert closure.outcome == "STOP LOSS"        # matched against the position's SL


@pytest.mark.asyncio
async def test_a_closure_whose_deal_never_arrives_is_reported_as_unknown():
    connection = FakeConnection()
    monitor, _ = await build(connection)

    await monitor.poll(DAY)                     # sync poll
    connection.positions = [position_record(669454576)]
    await monitor.poll(DAY)
    connection.positions = []

    for _ in range(TradeMonitor.DEAL_LOOKUP_ATTEMPTS):
        report = await monitor.poll(DAY)
    assert len(report.closed) == 1
    closure = report.closed[0]
    assert closure.pnl_known is False, "an unknown P&L must not masquerade as 0.00"
    assert closure.outcome == "CLOSED"


@pytest.mark.asyncio
async def test_a_closure_is_only_reported_once():
    connection = FakeConnection()
    monitor, _ = await build(connection)

    await monitor.poll(DAY)                     # sync poll
    connection.positions = [position_record(669454576)]
    await monitor.poll(DAY)
    connection.positions = []
    connection.deals = [deal_record(669454576, 12.50)]

    first = await monitor.poll(DAY)
    second = await monitor.poll(DAY)
    assert len(first.closed) == 1 and first.closed[0].pnl == pytest.approx(12.50)
    assert second.closed == []


# --- price conventions differ per payload --------------------------------

def test_position_prices_are_not_unscaled_twice():
    """get_positions returns real prices; get_spot_prices returns them x1e5.

    Dividing a position's 79851.50 entry a second time gave 0.80 - and so did
    its stop and its target, three different levels collapsing onto one number.
    """
    from utils.prices import Scale, normalize_price

    scale = Scale(100_000.0, "detected")
    for real in (79_851.50, 79_922.89, 79_637.53, 4_431.23):
        assert normalize_price(real, scale, 1_000, 1_000_000) == pytest.approx(real)
    # A genuinely scaled value still gets unscaled.
    assert normalize_price(7_985_150_000, scale, 1_000, 1_000_000) == pytest.approx(79_851.50)
    assert normalize_price(None, scale, 1_000, 1_000_000) is None
    assert normalize_price(0.0, scale, 1_000, 1_000_000) == 0.0


@pytest.mark.asyncio
async def test_a_position_read_back_keeps_its_real_prices():
    connection = FakeConnection()
    monitor, _ = await build(connection)
    await monitor.poll(DAY)                     # sync poll
    connection.positions = [{
        "positionId": 1, "symbolId": 10026, "symbolName": "BTCUSD",
        # BTCUSD is 1 BTC per lot, so 0.05 lots is 5 centi-units - not the 500
        # a 100oz gold lot would give.
        "tradeSide": "SELL", "volume": 5,
        # Real prices, exactly as the live server sends them for positions.
        "entryPrice": 79851.50, "stopLoss": 79922.89, "takeProfit": 79637.53,
        "label": "SB-BTCUSD-20260906-MORNING",
    }]
    report = await monitor.poll(DAY)
    position = report.positions[0]
    assert position.entry_price == pytest.approx(79_851.50)
    assert position.stop_loss == pytest.approx(79_922.89)
    assert position.take_profit == pytest.approx(79_637.53)
    assert position.volume == pytest.approx(0.05)      # 500 centi-units


@pytest.mark.asyncio
async def test_positions_open_before_boot_are_adopted_without_a_fill_alert():
    """Railway wipes the state file on every redeploy.

    Without this, each redeploy re-announced every open position as a fresh
    fill - which is exactly the alert that must stay trustworthy.
    """
    connection = FakeConnection()
    monitor, state = await build(connection)
    connection.positions = [position_record(669496943, label="SB-BTCUSD-MORNING")]

    first = await monitor.poll(DAY)
    assert first.filled == [], "an already-open position is not a new fill"
    assert "669496943" in state.known_position_ids

    # A genuinely new one, after the sync, still gets announced.
    connection.positions.append(position_record(669499999))
    second = await monitor.poll(DAY)
    assert [p.position_id for p in second.filled] == ["669499999"]
