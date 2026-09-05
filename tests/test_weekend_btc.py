"""Weekend BTCUSD trading: the schedule, the per-symbol profiles, and a live tick.

The point of these tests is that gold's numbers must not leak onto Bitcoin. A
point is ~85x smaller on gold, so gold's 35-point spread cap would reject every
BTC trade ever seen, and gold's 100 oz/lot contract size would size a BTC
position 100x too large.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")

import pytest

from config import load_config
from risk.guards import check_spread
from strategy.models import Direction
from strategy.silver_bullet import MarketSnapshot, SilverBulletStrategy
from symbols import BTCUSD, XAUUSD, profile_for, symbol_for_day
from tests import fixtures
from tests.fake_mcp import FakeConnection
from tests.test_integration import make_bot


# --- schedule -------------------------------------------------------------

@pytest.mark.parametrize("day,expected", [
    ("2026-09-04", "XAUUSD"),   # Friday
    ("2026-09-05", "BTCUSD"),   # Saturday
    ("2026-09-06", "BTCUSD"),   # Sunday
    ("2026-09-07", "XAUUSD"),   # Monday
])
def test_instrument_follows_the_day_of_week(day, expected):
    assert symbol_for_day(date.fromisoformat(day), weekday_symbol="XAUUSD",
                          weekend_symbol="BTCUSD", weekend_enabled=True) == expected


def test_weekend_trading_can_be_switched_off():
    assert symbol_for_day(date(2026, 9, 5), weekday_symbol="XAUUSD",
                          weekend_symbol="BTCUSD", weekend_enabled=False) is None


# --- profiles -------------------------------------------------------------

def test_golds_spread_guard_would_reject_every_btc_trade():
    """The regression this whole profile system exists to prevent."""
    live_btc = fixtures.btc_quote()          # 5.00 USD spread = 500 points
    assert not check_spread(live_btc, BTCUSD.point_size, XAUUSD.max_spread_points).allowed
    assert check_spread(live_btc, BTCUSD.point_size, BTCUSD.max_spread_points).allowed


def test_btc_profile_thresholds_are_scaled_for_btc():
    assert BTCUSD.contract_size == 1.0       # vs 100 oz/lot for gold
    for field in ("max_spread_points", "sl_buffer_points", "min_fvg_points",
                  "min_displacement_points", "equal_level_tolerance_points"):
        assert getattr(BTCUSD, field) > getattr(XAUUSD, field) * 10, field


def test_per_symbol_env_overrides_do_not_leak_across_symbols(monkeypatch):
    monkeypatch.setenv("CTRADER_MCP_TOKEN", "test-token-value")
    monkeypatch.setenv("MAX_SPREAD_POINTS", "35")        # legacy global
    monkeypatch.setenv("BTCUSD_MAX_SPREAD_POINTS", "900")
    cfg = load_config()
    assert cfg.profiles["XAUUSD"].max_spread_points == 35.0
    assert cfg.profiles["BTCUSD"].max_spread_points == 900.0


def test_unknown_symbol_falls_back_loudly():
    profile = profile_for("DOGEUSD")
    assert profile.name == "DOGEUSD"
    assert profile.contract_size == XAUUSD.contract_size   # gold-shaped, needs overrides


# --- strategy at BTC scale ------------------------------------------------

def test_the_same_setup_is_found_on_btc_with_btc_thresholds():
    strategy = SilverBulletStrategy(load_config(), BTCUSD)
    result = strategy.analyse(MarketSnapshot(
        entry_candles=fixtures.btc_m1_series(),
        structure_candles=fixtures.btc_m5_history(),
        htf_candles=fixtures.btc_h1_history(),
        quote=fixtures.btc_quote(),
        now=fixtures.BTC_NOW,
        window_name="AFTERNOON",
        window_start=fixtures.BTC_WINDOW_START,
    ))
    assert result.found, f"rejections: {result.rejections}"
    setup = result.setup
    assert setup.direction is Direction.SELL
    assert setup.entry == pytest.approx(79_532.50, abs=0.5)
    # 1000 points = 10 USD beyond the 80,493 sweep wick.
    assert setup.stop_loss == pytest.approx(80_503.00, abs=0.5)
    assert setup.take_profit == pytest.approx(77_195.00, abs=0.5)
    assert setup.risk_reward >= 2.0


def test_golds_thresholds_on_btc_data_produce_a_different_outcome():
    """Sanity: the profile genuinely changes the analysis, it is not decoration."""
    snapshot = MarketSnapshot(
        entry_candles=fixtures.btc_m1_series(),
        structure_candles=fixtures.btc_m5_history(),
        htf_candles=fixtures.btc_h1_history(),
        quote=fixtures.btc_quote(),
        now=fixtures.BTC_NOW,
        window_name="AFTERNOON",
        window_start=fixtures.BTC_WINDOW_START,
    )
    with_gold = SilverBulletStrategy(load_config(), XAUUSD).analyse(snapshot)
    with_btc = SilverBulletStrategy(load_config(), BTCUSD).analyse(snapshot)
    assert with_btc.found
    # Gold's 20-point (0.20 USD) buffer is meaningless on an 80,000 instrument.
    if with_gold.found:
        assert with_gold.setup.stop_loss != with_btc.setup.stop_loss


# --- full tick on a Saturday ---------------------------------------------

@pytest.mark.asyncio
async def test_saturday_tick_trades_btc_not_gold(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")
    monkeypatch.setattr("engine.orchestrator.now_utc", lambda: fixtures.BTC_NOW)

    await bot._tick()

    assert len(connection.orders) == 1
    order = connection.orders[0]
    assert order["symbolId"] == 10026                      # BTCUSD, not gold's 41
    assert order["tradeSide"] == "SELL"
    assert order["label"] == "SB-BTCUSD-20260905-AFTERNOON"
    assert order["limitPrice"] == pytest.approx(79_532.50, abs=0.5)
    assert order["stopLoss"] == pytest.approx(80_503.00, abs=0.5)
    # 1% of 10,000 = $100 behind a 970.50 stop at 1 BTC/lot -> 0.10 lots.
    assert order["volume"] == pytest.approx(0.10, abs=0.005)


@pytest.mark.asyncio
async def test_weekend_trading_disabled_means_no_market_contact(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live", WEEKEND_TRADING="false")
    monkeypatch.setattr("engine.orchestrator.now_utc", lambda: fixtures.BTC_NOW)

    await bot._tick()

    assert not connection.orders
    assert "get_trendbars" not in {name for name, _ in connection.calls}


@pytest.mark.asyncio
async def test_switching_days_switches_instrument_without_reconnecting(monkeypatch):
    connection = FakeConnection(balance=10_000.0)
    bot = make_bot(monkeypatch, connection, TRADING_MODE="live")

    monkeypatch.setattr("engine.orchestrator.now_utc", lambda: fixtures.NOW)      # Friday
    await bot._tick()
    monkeypatch.setattr("engine.orchestrator.now_utc", lambda: fixtures.BTC_NOW)  # Saturday
    await bot._tick()

    assert connection.stats.connects == 1, "switching instrument must not reconnect"
    assert [o["symbolId"] for o in connection.orders] == [41, 10026]
    assert bot.gateway.active_name == "BTCUSD"
    assert bot.strategy.profile.name == "BTCUSD"
