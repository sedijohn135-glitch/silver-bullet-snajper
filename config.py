"""Central configuration for the ICT Silver Bullet XAUUSD bot.

Every tunable lives here and is sourced from environment variables so the bot can
be reconfigured on Railway without touching code.  Values are validated eagerly at
start-up: a bot that silently trades with a mis-typed risk percentage is far more
dangerous than one that refuses to boot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Hard constants (deliberately NOT user configurable)
# ---------------------------------------------------------------------------

#: The one and only real cTrader MCP endpoint.  Do not parameterise this: a typo
#: in an env var must never be able to point live order flow at another host.
MCP_UPSTREAM = "https://mcp.ctrader.com/trading/mcp"

#: All Silver Bullet windows are evaluated in Albania local time.  ZoneInfo (not a
#: fixed UTC offset) so CET/CEST daylight-saving transitions are automatic.
LOCAL_TZ_NAME = "Europe/Tirane"


class ConfigError(RuntimeError):
    """Raised when the environment is not fit to start the bot."""


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _raw(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    # Strip inline comments that people commonly paste from .env examples,
    # e.g. TRADING_MODE=paper   # or "live"
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    value = value.strip().strip('"').strip("'")
    return value or None


def env_str(name: str, default: str = "") -> str:
    value = _raw(name)
    return default if value is None else value


def env_bool(name: str, default: bool = False) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float, *, minimum: Optional[float] = None,
              maximum: Optional[float] = None) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {parsed}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {parsed}")
    return parsed


def env_int(name: str, default: int, *, minimum: Optional[int] = None,
            maximum: Optional[int] = None) -> int:
    value = _raw(name)
    if value is None:
        return default
    try:
        parsed = int(float(value))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {parsed}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {parsed}")
    return parsed


def env_choice(name: str, default: str, choices: set[str]) -> str:
    value = (_raw(name) or default).lower()
    if value not in choices:
        raise ConfigError(f"{name} must be one of {sorted(choices)}, got {value!r}")
    return value


def env_json(name: str) -> dict:
    """A JSON object of literal upstream field names -> values.

    Escape hatch for a server build that requires a parameter this bot does not
    know about (e.g. ``{"accountId": 12345}``): it is merged verbatim into the
    ``create_order`` payload, so an exotic schema never needs a code change.
    """
    value = _raw(name)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} must be a JSON object, got {value!r}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{name} must be a JSON object, got {type(parsed).__name__}")
    return parsed


def env_scale(name: str) -> Optional[float]:
    """A price/money scale override.  ``auto`` (or unset) means auto-detect."""
    value = _raw(name)
    if value is None or value.lower() == "auto":
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be 'auto' or a number, got {value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be > 0")
    return parsed


# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    # --- connectivity -----------------------------------------------------
    mcp_token: str
    mcp_upstream: str = MCP_UPSTREAM
    mcp_call_timeout: float = 15.0
    mcp_connect_timeout: float = 20.0
    reconnect_base_delay: float = 2.0
    reconnect_max_delay: float = 60.0
    auth_failure_delay: float = 300.0

    # --- notifications ----------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- safety -----------------------------------------------------------
    trading_mode: str = "paper"          # paper | live
    allow_live_environment: bool = False  # permit a token whose env is "live"
    kill_switch: bool = False

    # --- instrument -------------------------------------------------------
    symbol: str = "XAUUSD"              # weekday instrument (kept for compatibility)
    weekend_symbol: str = "BTCUSD"      # traded Sat/Sun, when the weekday market is shut
    weekend_trading: bool = True
    profiles: dict = field(default_factory=dict)   # name -> SymbolProfile
    point_size: float = 0.01              # 1 point; 1 pip = 10 points on gold
    contract_size: float = 100.0          # ounces per 1.00 lot
    volume_step: float = 0.01
    min_volume: float = 0.01
    max_volume: float = 100.0
    volume_unit_mode: str = "auto"        # auto | lots | units | centiunits
    extra_order_fields: dict = field(default_factory=dict)  # literal create_order fields
    price_scale: Optional[float] = None   # None -> auto-detect
    money_scale: Optional[float] = None   # None -> auto-detect
    sane_price_min: float = 100.0
    sane_price_max: float = 100_000.0

    # --- risk -------------------------------------------------------------
    risk_per_trade_pct: float = 1.0
    daily_max_drawdown_pct: float = 3.0    # 0 disables the daily halt entirely
    min_rr: float = 2.0
    max_spread_points: float = 35.0
    sl_buffer_points: float = 20.0
    include_open_pnl_in_drawdown: bool = True
    min_account_balance: float = 0.0
    max_notional_leverage: float = 500.0  # sanity ceiling on position value
    fixed_lot_size: float = 0.0           # >0 pins the PER-LAYER lot size
    entry_layers: int = 1                 # split the entry into N laddered limits
    max_risk_pct: float = 10.0            # ceiling when fixed sizing is used (0=off)

    # --- strategy ---------------------------------------------------------
    entry_timeframe: str = "M1"
    structure_timeframe: str = "M5"
    htf_timeframe: str = "H1"
    entry_bars: int = 360
    structure_bars: int = 300
    htf_bars: int = 200
    swing_strength: int = 2
    displacement_body_mult: float = 1.6
    min_displacement_points: float = 30.0
    min_fvg_points: float = 8.0
    fvg_entry_mode: str = "proximal"      # proximal | mid | distal
    sweep_lookback_minutes: int = 45
    min_sweep_penetration_points: float = 3.0
    equal_level_tolerance_points: float = 15.0

    # --- execution --------------------------------------------------------
    poll_interval_seconds: float = 15.0
    order_expiry_minutes: int = 60
    cancel_unfilled_at_window_end: bool = True
    block_if_any_symbol_exposure: bool = True
    order_label_prefix: str = "SB"

    # --- ops --------------------------------------------------------------
    log_level: str = "INFO"
    health_port: Optional[int] = None
    state_dir: str = "/tmp/silver-bullet-state"

    # Derived helpers ------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    def points_to_price(self, points: float) -> float:
        return points * self.point_size

    def price_to_points(self, price_delta: float) -> float:
        return price_delta / self.point_size if self.point_size else 0.0


def load_config() -> Config:
    """Build (and validate) the runtime configuration from the environment."""
    token = env_str("CTRADER_MCP_TOKEN")
    if not token or token.startswith("replace-me"):
        raise ConfigError(
            "CTRADER_MCP_TOKEN is missing. Set it to the Bearer token issued for "
            "your cTrader MCP account (see .env.example)."
        )

    cfg = Config(
        mcp_token=token,
        mcp_call_timeout=env_float("MCP_CALL_TIMEOUT_SECONDS", 15.0, minimum=1.0, maximum=120.0),
        mcp_connect_timeout=env_float("MCP_CONNECT_TIMEOUT_SECONDS", 20.0, minimum=1.0, maximum=120.0),
        reconnect_base_delay=env_float("RECONNECT_BASE_DELAY_SECONDS", 2.0, minimum=0.5),
        reconnect_max_delay=env_float("RECONNECT_MAX_DELAY_SECONDS", 60.0, minimum=1.0),
        auth_failure_delay=env_float("AUTH_FAILURE_DELAY_SECONDS", 300.0, minimum=5.0),
        telegram_bot_token=env_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env_str("TELEGRAM_CHAT_ID"),
        trading_mode=env_choice("TRADING_MODE", "paper", {"paper", "live"}),
        allow_live_environment=env_bool("ALLOW_LIVE_ENVIRONMENT", False),
        kill_switch=env_bool("KILL_SWITCH", False),
        symbol=env_str("SYMBOL", "XAUUSD").upper(),
        weekend_symbol=env_str("WEEKEND_SYMBOL", "BTCUSD").upper(),
        weekend_trading=env_bool("WEEKEND_TRADING", True),
        point_size=env_float("POINT_SIZE", 0.01, minimum=1e-8),
        contract_size=env_float("CONTRACT_SIZE", 100.0, minimum=1e-8),
        volume_step=env_float("VOLUME_STEP", 0.01, minimum=1e-8),
        min_volume=env_float("MIN_VOLUME", 0.01, minimum=0.0),
        max_volume=env_float("MAX_VOLUME", 100.0, minimum=1e-8),
        volume_unit_mode=env_choice("VOLUME_UNIT_MODE", "auto",
                                    {"auto", "lots", "units", "centiunits"}),
        extra_order_fields=env_json("EXTRA_ORDER_FIELDS"),
        price_scale=env_scale("PRICE_SCALE"),
        money_scale=env_scale("MONEY_SCALE"),
        sane_price_min=env_float("SANE_PRICE_MIN", 100.0, minimum=0.0),
        sane_price_max=env_float("SANE_PRICE_MAX", 100_000.0, minimum=1.0),
        risk_per_trade_pct=env_float("RISK_PER_TRADE_PCT", 1.0, minimum=0.01, maximum=10.0),
        daily_max_drawdown_pct=env_float("DAILY_MAX_DRAWDOWN_PCT", 3.0, minimum=0.0, maximum=100.0),
        min_rr=env_float("MIN_RR", 2.0, minimum=0.1),
        max_spread_points=env_float("MAX_SPREAD_POINTS", 35.0, minimum=0.0),
        sl_buffer_points=env_float("SL_BUFFER_POINTS", 20.0, minimum=0.0),
        include_open_pnl_in_drawdown=env_bool("INCLUDE_OPEN_PNL_IN_DRAWDOWN", True),
        min_account_balance=env_float("MIN_ACCOUNT_BALANCE", 0.0, minimum=0.0),
        max_notional_leverage=env_float("MAX_NOTIONAL_LEVERAGE", 500.0, minimum=0.0),
        fixed_lot_size=env_float("FIXED_LOT_SIZE", 0.0, minimum=0.0),
        entry_layers=env_int("ENTRY_LAYERS", 1, minimum=1, maximum=20),
        max_risk_pct=env_float("MAX_RISK_PCT", 10.0, minimum=0.0),
        entry_timeframe=env_str("ENTRY_TIMEFRAME", "M1").upper(),
        structure_timeframe=env_str("STRUCTURE_TIMEFRAME", "M5").upper(),
        htf_timeframe=env_str("HTF_TIMEFRAME", "H1").upper(),
        entry_bars=env_int("ENTRY_BARS", 360, minimum=50, maximum=5000),
        structure_bars=env_int("STRUCTURE_BARS", 300, minimum=50, maximum=5000),
        htf_bars=env_int("HTF_BARS", 200, minimum=20, maximum=5000),
        swing_strength=env_int("SWING_STRENGTH", 2, minimum=1, maximum=10),
        displacement_body_mult=env_float("DISPLACEMENT_BODY_MULT", 1.6, minimum=1.0),
        min_displacement_points=env_float("MIN_DISPLACEMENT_POINTS", 30.0, minimum=0.0),
        min_fvg_points=env_float("MIN_FVG_POINTS", 8.0, minimum=0.0),
        fvg_entry_mode=env_choice("FVG_ENTRY_MODE", "proximal", {"proximal", "mid", "distal"}),
        sweep_lookback_minutes=env_int("SWEEP_LOOKBACK_MINUTES", 45, minimum=5, maximum=480),
        min_sweep_penetration_points=env_float("MIN_SWEEP_PENETRATION_POINTS", 3.0, minimum=0.0),
        equal_level_tolerance_points=env_float("EQUAL_LEVEL_TOLERANCE_POINTS", 15.0, minimum=0.0),
        poll_interval_seconds=env_float("POLL_INTERVAL_SECONDS", 15.0, minimum=1.0, maximum=300.0),
        order_expiry_minutes=env_int("ORDER_EXPIRY_MINUTES", 60, minimum=1, maximum=1440),
        cancel_unfilled_at_window_end=env_bool("CANCEL_UNFILLED_AT_WINDOW_END", True),
        block_if_any_symbol_exposure=env_bool("BLOCK_IF_ANY_SYMBOL_EXPOSURE", True),
        order_label_prefix=env_str("ORDER_LABEL_PREFIX", "SB"),
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
        health_port=env_int("PORT", 0) or None,
        state_dir=env_str("STATE_DIR", "/tmp/silver-bullet-state"),
    )

    if cfg.min_volume > cfg.max_volume:
        raise ConfigError("MIN_VOLUME must not exceed MAX_VOLUME")
    if cfg.sane_price_min >= cfg.sane_price_max:
        raise ConfigError("SANE_PRICE_MIN must be below SANE_PRICE_MAX")

    object.__setattr__(cfg, "profiles", _load_profiles(cfg))
    return cfg


#: Global env vars that used to configure the single symbol. They still work,
#: but they now apply **only to the weekday instrument** - applying a 35-point
#: spread cap to BTC would reject every trade it ever saw.
_LEGACY_GLOBALS: dict[str, str] = {
    "point_size": "POINT_SIZE",
    "contract_size": "CONTRACT_SIZE",
    "volume_step": "VOLUME_STEP",
    "min_volume": "MIN_VOLUME",
    "max_volume": "MAX_VOLUME",
    "max_spread_points": "MAX_SPREAD_POINTS",
    "sl_buffer_points": "SL_BUFFER_POINTS",
    "min_fvg_points": "MIN_FVG_POINTS",
    "min_displacement_points": "MIN_DISPLACEMENT_POINTS",
    "min_sweep_penetration_points": "MIN_SWEEP_PENETRATION_POINTS",
    "equal_level_tolerance_points": "EQUAL_LEVEL_TOLERANCE_POINTS",
    "sane_price_min": "SANE_PRICE_MIN",
    "sane_price_max": "SANE_PRICE_MAX",
}


def _load_profiles(cfg: "Config") -> dict:
    """Build the profile for every instrument this bot may trade.

    Precedence, lowest first: the built-in profile, then the legacy global env
    vars (weekday symbol only), then `<SYMBOL>_<FIELD>` overrides.
    """
    from symbols import OVERRIDABLE, apply_overrides, profile_for

    names = [n for n in (cfg.symbol, cfg.weekend_symbol) if n]
    profiles: dict = {}
    for name in dict.fromkeys(names):
        profile = profile_for(name)

        if name == cfg.symbol:
            legacy = {
                field_name: _raw(env_name)
                for field_name, env_name in _LEGACY_GLOBALS.items()
                if _raw(env_name) is not None
            }
            profile = apply_overrides(
                profile,
                {k: env_float(_LEGACY_GLOBALS[k], getattr(profile, k)) for k in legacy},
            )

        overrides = {}
        for field_name in OVERRIDABLE:
            env_name = f"{name}_{field_name.upper()}"
            if _raw(env_name) is not None:
                overrides[field_name] = env_float(env_name, getattr(profile, field_name))
        profiles[name] = apply_overrides(profile, overrides)
    return profiles
