"""Risk-layer tests: sizing arithmetic, drawdown accounting, and the gates."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")

import pytest

from mcp_client.token_info import TokenInfo
from models import Deal, PendingOrder, Position, Quote
from risk.daily import compute_daily_pnl
from risk.guards import (
    check_balance, check_daily_drawdown, check_environment, check_kill_switch,
    check_spread, check_window_exposure, first_blocker,
)
from risk.sizing import calculate_position_size

UTC = timezone.utc
DAY_START = datetime(2026, 9, 4, tzinfo=UTC)


def size(**overrides):
    payload = dict(
        balance=10_000.0, risk_pct=1.0, stop_distance=11.50,
        contract_size=100.0, volume_step=0.01, min_volume=0.01, max_volume=100.0,
    )
    payload.update(overrides)
    return calculate_position_size(**payload)


def test_one_percent_risk_is_sized_from_the_structural_stop():
    # 1% of 10,000 = $100 risk; a 11.50 stop on 100oz/lot risks $1150 per lot.
    result = size()
    assert result.accepted
    assert result.lots == pytest.approx(0.08)          # 0.0869 rounded DOWN
    assert result.actual_risk == pytest.approx(92.0)
    assert result.actual_risk <= result.risk_amount    # never rounds up into over-risk


def test_a_wider_stop_produces_a_smaller_position():
    tight, wide = size(stop_distance=5.0), size(stop_distance=25.0)
    assert tight.lots > wide.lots
    assert tight.actual_risk <= 100.0 and wide.actual_risk <= 100.0


def test_size_below_the_broker_minimum_is_refused_not_rounded_up():
    """Taking the minimum lot would breach the risk limit, so we take nothing."""
    result = size(balance=200.0, stop_distance=30.0)
    assert not result.accepted
    assert "below the 0.01 minimum" in result.reason


def test_sizing_rejects_nonsense_inputs():
    assert not size(balance=0).accepted
    assert not size(stop_distance=0).accepted


def test_daily_pnl_uses_broker_deals_and_open_positions():
    deals = [
        Deal("1", 41, "p1", 0.1, 4400.0, -120.0, executed_at=DAY_START + timedelta(hours=9)),
        Deal("2", 41, "p2", 0.1, 4400.0, +40.0, executed_at=DAY_START + timedelta(hours=10)),
        Deal("old", 41, "p0", 0.1, 4400.0, -900.0, executed_at=DAY_START - timedelta(hours=5)),
    ]
    positions = [Position("p3", 41, "XAUUSD", "SELL", 0.1, 4420.0, profit=-30.0)]
    pnl = compute_daily_pnl(balance=9_920.0, deals=deals, positions=positions,
                            day_start=DAY_START, max_drawdown_pct=3.0)
    assert pnl.realized == pytest.approx(-80.0)       # yesterday's loss excluded
    assert pnl.unrealized == pytest.approx(-30.0)
    assert pnl.day_start_balance == pytest.approx(10_000.0)
    assert pnl.loss_pct == pytest.approx(1.10)
    assert not pnl.halted


def test_daily_drawdown_limit_halts_trading():
    deals = [Deal("1", 41, "p1", 1.0, 4400.0, -305.0, executed_at=DAY_START + timedelta(hours=9))]
    pnl = compute_daily_pnl(balance=9_695.0, deals=deals, positions=[],
                            day_start=DAY_START, max_drawdown_pct=3.0)
    assert pnl.halted
    assert check_daily_drawdown(pnl).allowed is False


def test_spread_guard_uses_points_not_pips():
    point = 0.01
    assert check_spread(Quote(41, 4431.23, 4431.55), point, 35).allowed          # 32pt
    assert not check_spread(Quote(41, 4431.23, 4431.63), point, 35).allowed      # 40pt
    assert not check_spread(Quote(41, 4431.60, 4431.20), point, 35).allowed      # inverted


def test_environment_guard_requires_two_switches_for_a_live_account():
    demo = TokenInfo("demo", "icmarkets", decoded=True)
    live = TokenInfo("live", "icmarkets", decoded=True)
    unknown = TokenInfo(None, None)

    assert not check_environment("paper", demo, False).allowed   # paper never sends
    assert check_environment("live", demo, False).allowed        # demo + live mode is fine
    assert not check_environment("live", live, False).allowed    # live account needs opt-in
    assert check_environment("live", live, True).allowed
    assert not check_environment("live", unknown, False).allowed  # unknown is treated as live


def test_one_trade_per_window_is_verified_against_the_broker():
    label = "SB-20260904-AFTERNOON"
    existing = [PendingOrder("o1", 41, "XAUUSD", "SELL", 0.08, 4419.5, label=label)]
    blocked = check_window_exposure([], existing, window_label=label, symbol_id=41,
                                    symbol_name="XAUUSD", block_any_symbol_exposure=False)
    assert not blocked.allowed and blocked.code == "one_per_window"

    other_window = check_window_exposure([], existing, window_label="SB-20260904-EVENING",
                                         symbol_id=41, symbol_name="XAUUSD",
                                         block_any_symbol_exposure=False)
    assert other_window.allowed


def test_any_symbol_exposure_blocks_when_configured():
    positions = [Position("p1", 41, "XAUUSD", "BUY", 0.1, 4400.0, label="manual-trade")]
    verdict = check_window_exposure(positions, [], window_label="SB-20260904-EVENING",
                                    symbol_id=41, symbol_name="XAUUSD",
                                    block_any_symbol_exposure=True)
    assert not verdict.allowed and verdict.code == "symbol_exposure"


def test_first_blocker_reports_the_earliest_failure():
    verdicts = [
        check_kill_switch(False),
        check_balance(10_000.0, 0.0),
        check_spread(Quote(41, 4431.23, 4431.99), 0.01, 35),
    ]
    blocker = first_blocker(verdicts)
    assert blocker is not None and blocker.code == "spread"
    assert first_blocker([check_kill_switch(False), check_balance(500.0, 0.0)]) is None
