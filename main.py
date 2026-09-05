"""ICT Silver Bullet XAUUSD bot - process entry point.

Boot sequence:
  1. load and validate configuration (fail fast on a bad environment);
  2. decode the Bearer token and log which cTrader environment it points at;
  3. open the supervised MCP session and discover the live tool set;
  4. run the 24/7 control loop until SIGTERM (Railway's redeploy signal).
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional

from config import Config, ConfigError, load_config
from engine.orchestrator import SilverBulletBot
from mcp_client.token_info import TokenInfo, decode_token
from utils.health import HealthServer
from utils.logging import get_logger, register_secret, setup_logging
from utils.telegram import NullNotifier, TelegramNotifier

log = get_logger("main")

BANNER = r"""
  ___ ___ _____   ___ _ _              ___      _ _     _
 |_ _/ __|_   _| / __(_) |_ _____ _ _ | _ )_  _| | |___| |_
  | | (__  | |   \__ \ | \ V / -_) '_|| _ \ || | | / -_)  _|
 |___\___| |_|   |___/_|_|\_/\___|_|  |___/\_,_|_|_\___|\__|
              XAUUSD - Europe/Tirane windows
"""


def build_notifier(cfg: Config) -> TelegramNotifier:
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        return TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    log.warning("Telegram is not configured; alerts will only go to the log.")
    return NullNotifier()


def announce_instruments(cfg: Config) -> None:
    """Print the trading calendar and the exact contract mechanics in use.

    CONTRACT_SIZE cannot be read from the API - the symbol listing does not
    publish it - so it is printed here for the operator to check against the
    broker's symbol specification before any order is sized from it.
    """
    weekend = f"{cfg.weekend_symbol} Sat/Sun" if cfg.weekend_trading else "idle at weekends"
    log.info("Trading calendar: %s Mon-Fri, %s", cfg.symbol, weekend)
    for profile in (cfg.profiles or {}).values():
        log.info("  profile | %s", profile.describe())


def announce_environment(cfg: Config, token: TokenInfo) -> None:
    """Make the account environment impossible to miss in the logs.

    This is the single most important line in the boot sequence: it is how an
    operator confirms, before any order can be sent, whether the connected
    cTrader account is DEMO or LIVE.
    """
    log.info("cTrader account %s", token.describe())
    log.info("Token claims: %s", token.safe_claims())

    if cfg.is_live and token.is_live and not cfg.allow_live_environment:
        log.warning(
            "=" * 78 + "\n"
            "  TRADING_MODE=live but the token points at a LIVE account and\n"
            "  ALLOW_LIVE_ENVIRONMENT is not set. Orders will be SIMULATED.\n"
            "  Set ALLOW_LIVE_ENVIRONMENT=true only if you intend to trade real money.\n"
            + "=" * 78
        )
    elif cfg.is_live and token.is_live:
        log.warning(
            "=" * 78 + "\n"
            "  LIVE ACCOUNT + LIVE MODE: this process will place REAL orders.\n"
            + "=" * 78
        )
    elif cfg.is_live and token.is_demo:
        log.info("Live order execution enabled against a DEMO account.")
    elif cfg.is_live and token.is_unknown:
        log.warning(
            "Token environment could not be decoded; treating the account as LIVE. "
            "Orders stay simulated unless ALLOW_LIVE_ENVIRONMENT=true."
        )
    else:
        log.info("TRADING_MODE=paper: analysis runs in full, no orders are sent.")


async def run() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Configuration error: %s", exc)
        return 2

    setup_logging(cfg.log_level)
    register_secret(cfg.mcp_token)
    register_secret(cfg.telegram_bot_token)
    for line in BANNER.strip("\n").splitlines():
        log.info(line)

    token = decode_token(cfg.mcp_token)
    announce_environment(cfg, token)
    announce_instruments(cfg)

    notifier = build_notifier(cfg)
    await notifier.start()

    bot = SilverBulletBot(cfg, token, notifier)
    health: Optional[HealthServer] = None
    if cfg.health_port:
        health = HealthServer(cfg.health_port, bot.snapshot)
        await health.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop(signame: str) -> None:
        log.info("Received %s; shutting down gracefully.", signame)
        stop.set()

    for signame in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, signame),
                                    _request_stop, signame)
        except (NotImplementedError, AttributeError):  # pragma: no cover - non-POSIX
            pass

    runner = asyncio.create_task(bot.run(), name="bot")
    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        await bot.stop()
        runner.cancel()
        stopper.cancel()
        for task in (runner, stopper):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if health is not None:
            await health.stop()
        await notifier.close()
    log.info("Shutdown complete.")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        return 0


if __name__ == "__main__":
    sys.exit(main())
