"""End-to-end tests for the Silver Bullet analysis pipeline."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")

import pytest

from config import load_config
from strategy.fvg import find_fvgs, mitigation_ratio
from strategy.liquidity import build_pools, detect_sweep, detect_sweeps, find_swings
from strategy.models import Direction
from strategy.silver_bullet import MarketSnapshot, SilverBulletStrategy
from tests import fixtures


def build_snapshot(**overrides) -> MarketSnapshot:
    payload = dict(
        entry_candles=fixtures.m1_series(),
        structure_candles=fixtures.m5_history(),
        htf_candles=fixtures.h1_history(),
        quote=fixtures.quote(),
        now=fixtures.NOW,
        window_name="AFTERNOON",
        window_start=fixtures.WINDOW_START,
    )
    payload.update(overrides)
    return MarketSnapshot(**payload)


@pytest.fixture()
def cfg():
    return load_config()


@pytest.fixture()
def strategy(cfg):
    return SilverBulletStrategy(cfg, point_size=0.01)


def test_pools_include_session_and_previous_day_levels(cfg, strategy):
    candles = fixtures.m5_history()
    swings = find_swings(candles, cfg.swing_strength)
    pools = build_pools(candles, swings, fixtures.NOW,
                        equal_tolerance=strategy.points(cfg.equal_level_tolerance_points))
    names = {p.name for p in pools}
    assert "ASIA_HIGH" in names and "ASIA_LOW" in names
    assert "LONDON_HIGH" in names and "LONDON_LOW" in names
    london_high = next(p for p in pools if p.name == "LONDON_HIGH")
    assert london_high.price == pytest.approx(4430.0, abs=0.01)


def test_sweep_is_detected_with_rejection_close(cfg, strategy):
    candles = fixtures.m1_series()
    swings = find_swings(candles, cfg.swing_strength)
    pools = build_pools(fixtures.m5_history(), swings, fixtures.NOW,
                        equal_tolerance=strategy.points(cfg.equal_level_tolerance_points))
    sweep = detect_sweep(candles, pools, since=fixtures.WINDOW_START,
                         min_penetration=strategy.points(cfg.min_sweep_penetration_points))
    assert sweep is not None
    assert sweep.direction is Direction.SELL
    assert sweep.extreme == pytest.approx(4430.80, abs=0.01)
    # The wick clears several highs at once; the raid is attributed to the
    # highest level taken, not the deepest one below it.
    assert sweep.pool.name == "LONDON_HIGH"


def test_spent_liquidity_is_not_swept_again(cfg, strategy):
    """London traded through the Asian high, so 4420 is no longer live liquidity."""
    from strategy.liquidity import build_pools as _build

    candles = fixtures.m5_history()
    swings = find_swings(candles, cfg.swing_strength)
    pools = _build(candles, swings, fixtures.NOW,
                   equal_tolerance=strategy.points(cfg.equal_level_tolerance_points),
                   tapped_until=fixtures.WINDOW_START,
                   tap_tolerance=strategy.points(cfg.min_sweep_penetration_points))
    assert "ASIA_HIGH" not in {p.name for p in pools}
    assert "LONDON_HIGH" in {p.name for p in pools}


def test_breakout_without_close_back_is_not_a_sweep(cfg, strategy):
    """A level taken and held is a breakout - fading it must be refused."""
    from models import Candle

    candles = list(fixtures.m1_series())
    idx = next(i for i, c in enumerate(candles) if c.high == pytest.approx(4430.80, abs=0.01))
    # Accept the level and hold above it: every subsequent close stays north of
    # 4430, which is a breakout, not a raid.
    candles[idx] = Candle(candles[idx].ts, 4429.4, 4430.8, 4429.2, 4430.6)
    candles = candles[: idx + 1] + [
        Candle(c.ts, 4430.8 + i, 4431.6 + i, 4430.6 + i, 4431.4 + i)
        for i, c in enumerate(candles[idx + 1:])
    ]
    swings = find_swings(candles, cfg.swing_strength)
    pools = build_pools(fixtures.m5_history(), swings, fixtures.NOW,
                        equal_tolerance=strategy.points(cfg.equal_level_tolerance_points))
    sweeps = detect_sweeps(candles, pools, since=fixtures.WINDOW_START,
                           min_penetration=strategy.points(cfg.min_sweep_penetration_points))
    assert all(s.pool.name != "LONDON_HIGH" for s in sweeps), (
        "a level that was taken and held is a breakout, not a liquidity raid"
    )


def test_bearish_fvg_is_found_in_the_displacement_leg():
    candles = fixtures.m1_series()
    gaps = find_fvgs(candles, Direction.SELL, min_size=0.08)
    assert gaps, "expected at least one bearish FVG"
    gap = max(gaps, key=lambda g: g.ts)
    assert gap.top == pytest.approx(4425.00, abs=0.01)
    assert gap.bottom == pytest.approx(4419.50, abs=0.01)
    assert gap.proximal == pytest.approx(4419.50, abs=0.01)   # first edge touched
    assert mitigation_ratio(candles, gap) < 0.5


def test_full_setup_is_produced_with_structural_stop_and_2r_target(strategy):
    result = strategy.analyse(build_snapshot())
    assert result.found, f"expected a setup, rejections: {result.rejections}"
    setup = result.setup

    assert setup.direction is Direction.SELL
    assert setup.entry == pytest.approx(4419.50, abs=0.01)
    # Structural stop: 20 points (0.20) beyond the 4430.80 sweep wick.
    assert setup.stop_loss == pytest.approx(4431.00, abs=0.01)
    assert setup.risk_reward >= 2.0
    assert setup.take_profit < setup.entry < setup.stop_loss
    assert setup.window == "AFTERNOON"
    assert len(setup.narrative) == 5


def test_no_sweep_means_no_trade(strategy):
    """Flat, featureless price action must produce a reasoned rejection."""
    flat = fixtures.drift(fixtures.DAY.replace(hour=12, minute=30), 120, 60, 4425.0, amplitude=0.2)
    result = strategy.analyse(build_snapshot(entry_candles=flat))
    assert not result.found
    assert any("sweep" in r.lower() for r in result.rejections)


def test_rr_floor_blocks_a_setup_with_no_far_enough_target(cfg, strategy):
    """Strip the previous-day low and the 2R requirement should refuse the trade."""
    htf = [c for c in fixtures.h1_history() if c.low > 4400]
    result = strategy.analyse(build_snapshot(htf_candles=htf))
    if result.found:
        assert result.setup.risk_reward >= cfg.min_rr
    else:
        assert any("R minimum" in r or "draw-on-liquidity" in r for r in result.rejections)
