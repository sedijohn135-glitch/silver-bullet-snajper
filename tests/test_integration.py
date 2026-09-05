"""Full-stack tests: fake MCP server -> gateway -> strategy -> risk -> executor."""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")

import pytest

from config import load_config
from engine.orchestrator import SilverBulletBot
from mcp_client.ctrader import CTraderGateway
from mcp_client.token_info import TokenInfo
from tests import fixtures
from tests.fake_mcp import FakeConnection
from utils.telegram import NullNotifier


def make_bot(monkeypatch, connection: FakeConnection, **env) -> SilverBulletBot:
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("CTRADER_MCP_TOKEN", "test-token-value")
    cfg = load_config()
    token = TokenInfo("demo", "icmarkets", decoded=True)
    bot = SilverBulletBot(cfg, token, NullNotifier())
    bot.connection = connection
    bot.gateway = CTraderGateway(cfg, connection)
    bot.monitor._gateway = bot.gateway
    bot.executor._gateway = bot.gateway
    # Freeze the clock inside the AFTERNOON window (16:10 Europe/Tirane).
    monkeypatch.setattr("engine.orchestrator.now_utc", lambda: fixtures.NOW)
    monkeypatch.setattr("engine.state.StateStore.save", lambda self, state: None)
    return bot


@pytest.mark.asyncio
async def test_gateway_unscales_prices_and_money():
    connection = FakeConnection(balance=10_000.0)
    gateway = CTraderGateway(load_config(), connection)
    await gateway.bootstrap()

    assert gateway.symbol.symbol_id == 41
    assert gateway.price_scale.divisor == 100_000        # detected, not configured
    assert gateway.money_scale.divisor == 100

    quote = await gateway.get_quote()
    assert quote.bid == pytest.approx(4418.10, abs=0.01)
    assert quote.ask == pytest.approx(4418.40, abs=0.01)

    balance = await gateway.get_balance()
    assert balance.balance == pytest.approx(10_000.0)

    candles = await gateway.get_candles("M1", 200)
    assert candles[-1].close == pytest.approx(4418.20, abs=0.01)
    assert candles[0].ts < candles[-1].ts


@pytest.mark.asyncio
async def test_trendbar_request_uses_the_period_enum_the_server_declares():
    connection = FakeConnection()
    gateway = CTraderGateway(load_config(), connection)
    await gateway.bootstrap()
    await gateway.get_candles("M1", 100)
    period_args = [args for name, args in connection.calls if name == "get_trendbars"]
    assert period_args and period_args[0]["period"] == "M_1"   # not "M1"
    assert "fromTimestamp" in period_args[0] and "toTimestamp" in period_args[0]


@pytest.mark.asyncio
async def test_paper_mode_runs_the_whole_pipeline_without_sending_an_order(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="paper")

    await bot._tick()

    assert not connection.orders, "paper mode must never call create_order"
    label = "SB-XAUUSD-20260904-AFTERNOON"
    assert label in bot.state.executed_windows
    assert bot.state.trades_today == 1


@pytest.mark.asyncio
async def test_live_mode_submits_a_schema_bound_limit_order(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")

    await bot._tick()

    assert len(connection.orders) == 1
    order = connection.orders[0]
    assert order["symbolId"] == 41
    assert order["orderType"] == "LIMIT"
    assert order["tradeSide"] == "SELL"
    assert order["limitPrice"] == pytest.approx(4419.50, abs=0.01)
    assert order["stopLoss"] == pytest.approx(4431.00, abs=0.01)     # 20pt beyond the wick
    assert order["takeProfit"] == pytest.approx(4392.00, abs=0.01)
    assert order["label"] == "SB-XAUUSD-20260904-AFTERNOON"
    # 1% of 10,000 = $100 behind an 11.50 stop on 100oz/lot -> 0.08 lots.
    # The live schema declares volume as an integer, so it goes on the wire as
    # cTrader centi-units: 0.08 lots x 100 oz x 100 = 800.
    assert order["volume"] == 800
    assert isinstance(order["volume"], int)


@pytest.mark.asyncio
async def test_one_trade_per_window_is_enforced_across_ticks(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")

    await bot._tick()
    await bot._tick()
    await bot._tick()

    assert len(connection.orders) == 1, "the window lock must survive repeated ticks"


@pytest.mark.asyncio
async def test_a_redeployed_bot_sees_its_own_resting_order(monkeypatch):
    """In-memory state is gone, but the broker still holds the labelled order."""
    resting = [{
        "orderId": 5001, "symbolId": 41, "symbolName": "XAUUSD", "tradeSide": "SELL",
        "volume": 800, "limitPrice": 441950000, "label": "SB-XAUUSD-20260904-AFTERNOON",
        "orderType": "LIMIT",
    }]
    connection = FakeConnection(balance=10_000.0, pending=resting)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")

    await bot._tick()

    assert not connection.orders, "must not duplicate an order it forgot about"
    assert not connection.cancelled, "the current window's order must not be cancelled"


@pytest.mark.asyncio
async def test_wide_spread_blocks_entry(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live", MAX_SPREAD_POINTS=10)

    await bot._tick()

    assert not connection.orders
    assert "Spread" in bot.state.halted_reason


@pytest.mark.asyncio
async def test_daily_drawdown_halts_the_day(monkeypatch):
    losses = [{
        "dealId": "d1", "symbolId": 41, "positionId": "p1", "volume": 100,
        "netProfit": -31_000,          # cents => -310.00 on a 10,000 opening balance
        "executionTimestamp": int((fixtures.DAY + timedelta(hours=9)).timestamp() * 1000),
    }]
    connection = FakeConnection(balance=9_690.0, deals=losses)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")

    await bot._tick()

    assert not connection.orders
    assert "Daily drawdown" in bot.state.halted_reason


@pytest.mark.asyncio
async def test_outside_the_window_nothing_is_analysed(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")
    # 12:10 Tirane - between the morning and afternoon windows.
    monkeypatch.setattr("engine.orchestrator.now_utc",
                        lambda: fixtures.NOW.replace(hour=10, minute=10))

    await bot._tick()

    assert not connection.orders
    assert "get_trendbars" not in {name for name, _ in connection.calls}


@pytest.mark.asyncio
async def test_stale_order_from_an_earlier_window_is_cancelled(monkeypatch):
    stale = [{
        "orderId": 4001, "symbolId": 41, "symbolName": "XAUUSD", "tradeSide": "SELL",
        "volume": 500, "limitPrice": 442000000, "label": "SB-XAUUSD-20260904-MORNING",
        "orderType": "LIMIT",
    }]
    connection = FakeConnection(balance=10_000.0, pending=stale)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")

    await bot._tick()

    assert connection.cancelled == ["4001"]
