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

    connection.positions = [position_record(669454576, label="")]   # no label
    report = await monitor.poll(DAY)

    assert [p.position_id for p in report.filled] == ["669454576"]


@pytest.mark.asyncio
async def test_a_closure_with_no_deal_yet_is_deferred_not_reported_as_zero():
    """The closing deal lands in get_deals a moment after the position vanishes."""
    connection = FakeConnection()
    monitor, _ = await build(connection)

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

    connection.positions = [position_record(669454576)]
    await monitor.poll(DAY)
    connection.positions = []
    connection.deals = [deal_record(669454576, 12.50)]

    first = await monitor.poll(DAY)
    second = await monitor.poll(DAY)
    assert len(first.closed) == 1 and first.closed[0].pnl == pytest.approx(12.50)
    assert second.closed == []
