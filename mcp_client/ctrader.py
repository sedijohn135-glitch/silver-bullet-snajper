"""High-level cTrader operations over the discovered MCP tool set.

Every method here resolves its tool by name *aliases* against the live catalogue
and builds its arguments from the live ``inputSchema``.  Nothing about the wire
format is assumed - not the tool names, not the parameter names, not the enum
spellings, not even whether volume is expressed in lots or units.

Unit normalisation happens exactly once, at this boundary: everything the layers
above see is already in human units (price 4431.23, balance 10000.00, lots 0.42).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from config import Config
from mcp_client.errors import ToolCallError, ToolUnavailable
from mcp_client.parsing import (
    as_float, as_int, collect_numbers, extract_payload, find_list,
    normalize_side, parse_timestamp, pick,
)
from mcp_client.schema import (
    Candidates, ToolSpec, bind_arguments, declared_types, normalize, wants_units,
)
from mcp_client.transport import MCPConnection
from models import (
    AccountSnapshot, Candle, Deal, PendingOrder, Position, Quote, SymbolInfo,
)
from symbols import SymbolProfile, profile_for
from utils.logging import get_logger
from utils.prices import Scale, resolve_money_scale, resolve_price_scale
from utils.timeframes import get_timeframe

log = get_logger("ctrader")

# Tool-name aliases, best-guess first.  ``create_order`` is the real order entry
# point - there is deliberately no "place_limit_order" here, that tool does not
# exist on the cTrader MCP server.
TOOL_BALANCE = ("get_balance", "getBalance", "get_account_info", "get_account", "get_trader")
TOOL_SYMBOLS = ("get_symbols", "getSymbols", "list_symbols", "symbols")
TOOL_SPOT = ("get_spot_prices", "get_spot_price", "getSpotPrices", "get_quotes", "get_prices")
TOOL_TRENDBARS = ("get_trendbars", "getTrendbars", "get_candles", "get_bars", "get_ohlc")
TOOL_POSITIONS = ("get_positions", "getPositions", "list_positions", "get_open_positions")
TOOL_PENDING = ("get_pending_orders", "getPendingOrders", "list_pending_orders", "get_orders")
TOOL_DEALS = ("get_deals", "getDeals", "list_deals", "get_trade_history")
TOOL_ORDER_HISTORY = ("get_order_history", "getOrderHistory", "order_history")
TOOL_CREATE_ORDER = ("create_order", "createOrder", "place_order", "new_order", "submit_order")
TOOL_CANCEL_ORDER = ("cancel_order", "cancelOrder", "delete_order")
TOOL_CLOSE_POSITION = ("close_position", "closePosition")
TOOL_AMEND_POSITION = ("amend_position", "amendPosition", "modify_position")
TOOL_AMEND_ORDER = ("amend_order", "amendOrder", "modify_order")
TOOL_VERSION = ("get_version", "getVersion", "version")

#: Wire spellings we offer for BUY/SELL; the binder keeps whichever the enum has.
SIDE_CANDIDATES = {
    "BUY": Candidates.of("BUY", "Buy", "buy", "LONG", "long", 1),
    "SELL": Candidates.of("SELL", "Sell", "sell", "SHORT", "short", 2),
}
ORDER_TYPE_CANDIDATES = {
    "LIMIT": Candidates.of("LIMIT", "Limit", "limit", "LIMIT_ORDER", 2),
    "MARKET": Candidates.of("MARKET", "Market", "market", "MARKET_ORDER", 1),
    "STOP": Candidates.of("STOP", "Stop", "stop", 3),
}


@dataclass
class OrderRequest:
    """A fully-formed order, in human units, ready to be schema-bound."""

    side: str                 # "BUY" | "SELL"
    order_type: str           # "LIMIT" | "MARKET"
    volume_lots: float
    price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    label: str
    comment: str = ""
    expiry: Optional[datetime] = None


class CTraderGateway:
    """Typed facade over the MCP tool set."""

    def __init__(self, cfg: Config, connection: MCPConnection) -> None:
        self._cfg = cfg
        self._conn = connection
        self.active_name: str = cfg.symbol
        self.symbols: dict[str, SymbolInfo] = {}
        self.money_scale = Scale(1.0, "fallback")
        self._scales: dict[str, Scale] = {}
        self._point_sizes: dict[str, float] = {}
        self._volume_mode: str = cfg.volume_unit_mode
        self._warned: set[str] = set()

    # -- active instrument -------------------------------------------------

    @property
    def symbol(self) -> Optional[SymbolInfo]:
        return self.symbols.get(self.active_name)

    @property
    def profile(self) -> SymbolProfile:
        """Contract mechanics and point thresholds for the active instrument."""
        profiles = self._cfg.profiles or {}
        return profiles.get(self.active_name) or profile_for(self.active_name)

    @property
    def price_scale(self) -> Scale:
        return self._scales.get(self.active_name, Scale(1.0, "fallback"))

    @property
    def point_size(self) -> float:
        return self._point_sizes.get(self.active_name, self.profile.point_size)

    def set_active(self, name: str) -> None:
        """Switch the instrument the gateway operates on."""
        name = name.upper()
        if name == self.active_name and name in self.symbols:
            return
        if name not in self.symbols:
            raise ToolCallError(
                f"{name} was not resolved at bootstrap "
                f"(known: {sorted(self.symbols)}); cannot make it active."
            )
        self.active_name = name
        log.info("Active instrument -> %s (id %s) | %s",
                 name, self.symbols[name].symbol_id, self.profile.describe())

    # -- plumbing ----------------------------------------------------------

    async def _call(self, aliases: Sequence[str], values: dict[str, Any],
                    *, extras: Optional[dict[str, Any]] = None,
                    strict: bool = True, timeout: Optional[float] = None) -> Any:
        tool = self._conn.catalog.require(*aliases)
        arguments = bind_arguments(tool, values, extras=extras, strict=strict)
        log.debug("call %s %s", tool.name, arguments)
        result = await self._conn.call_tool(tool.name, arguments, timeout=timeout)
        return extract_payload(result)

    def _tool(self, aliases: Sequence[str]) -> Optional[ToolSpec]:
        try:
            return self._conn.catalog.find(*aliases)
        except Exception:  # noqa: BLE001 - catalogue may not exist yet
            return None

    def has(self, aliases: Sequence[str]) -> bool:
        return self._tool(aliases) is not None

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    # -- bootstrap ---------------------------------------------------------

    async def bootstrap(self) -> None:
        """Resolve every configured instrument and pin down scaling.

        Runs after each (re)connect, before any sizing decision: a wrong scale
        silently turns a 1% risk into a 1000% one. Both the weekday and weekend
        instruments are resolved up front so the daily switch costs nothing.
        """
        await self._detect_money_scale()

        wanted = list(self._cfg.profiles or {self._cfg.symbol: None})
        for name in wanted:
            profile = (self._cfg.profiles or {}).get(name) or profile_for(name)
            info = await self.resolve_symbol(name)
            self.symbols[name] = info
            self._point_sizes[name] = self._resolve_point_size(info, profile)
            self._scales[name] = await self._detect_price_scale(info, profile)
            log.info("Instrument ready | %s (id %s) | %s",
                     info.name, info.symbol_id, profile.describe())

        if self.active_name not in self.symbols and self.symbols:
            self.active_name = next(iter(self.symbols))

    def _resolve_point_size(self, info: SymbolInfo, profile: SymbolProfile) -> float:
        """Prefer the broker's own digits; fall back to the profile."""
        if info.digits:
            derived = 10.0 ** (-info.digits)
            if not math.isclose(derived, profile.point_size, rel_tol=1e-9):
                log.info("%s point size from broker digits=%s -> %s (profile said %s)",
                         info.name, info.digits, derived, profile.point_size)
            return derived
        log.info("%s: broker exposed no 'digits'; using profile point size %s",
                 info.name, profile.point_size)
        return profile.point_size

    async def _detect_price_scale(self, info: SymbolInfo, profile: SymbolProfile) -> Scale:
        payload = await self._raw_spot(info)
        samples = collect_numbers(find_list(payload, "prices", "spots", "quotes"),
                                  ("bid", "ask", "price"), limit=6)
        scale = resolve_price_scale(
            self._cfg.price_scale, samples, profile.sane_price_min, profile.sane_price_max
        )
        example = (samples[0] / scale.divisor) if samples else float("nan")
        log.info("%s price scale: divide by %s (%s) - e.g. %s -> %.2f",
                 info.name, scale.divisor, scale.source,
                 samples[0] if samples else "n/a", example)
        return scale

    async def _detect_money_scale(self) -> None:
        if not self.has(TOOL_BALANCE):
            self._warn_once("no-balance", "No balance tool exposed; money scale left at 1.0")
            return
        payload = await self._call(TOOL_BALANCE, {})
        record = payload if isinstance(payload, dict) else (find_list(payload) or [{}])[0]
        samples = [
            v for v in (
                as_float(pick(record, "balance", "accountBalance"), None),
                as_float(pick(record, "equity"), None),
            ) if v
        ]
        self.money_scale = resolve_money_scale(self._cfg.money_scale, samples)
        log.info("Money scale: divide by %s (%s) - balance %s -> %.2f",
                 self.money_scale.divisor, self.money_scale.source,
                 samples[0] if samples else "n/a",
                 (samples[0] / self.money_scale.divisor) if samples else float("nan"))

    # -- reference data ----------------------------------------------------

    async def resolve_symbol(self, name: str) -> SymbolInfo:
        payload = await self._call(TOOL_SYMBOLS, {})
        records = find_list(payload, "symbols", "data")
        target = normalize(name)
        best: Optional[SymbolInfo] = None
        for record in records:
            symbol_name = str(pick(record, "symbolName", "name", "symbol", default=""))
            if not symbol_name:
                continue
            info = SymbolInfo(
                symbol_id=as_int(pick(record, "symbolId", "id"), -1) or -1,
                name=symbol_name,
                description=str(pick(record, "description", default="")),
                digits=as_int(pick(record, "digits", "pipPosition"), None),
                lot_size=as_float(pick(record, "lotSize", "contractSize"), None),
                min_volume=as_float(pick(record, "minVolume", "volumeMin"), None),
                max_volume=as_float(pick(record, "maxVolume", "volumeMax"), None),
                volume_step=as_float(pick(record, "stepVolume", "volumeStep"), None),
                raw=record if isinstance(record, dict) else {},
            )
            if normalize(symbol_name) == target:
                return info
            if best is None and target in normalize(symbol_name):
                best = info
        if best is not None:
            log.warning("Exact symbol %s not found; using closest match %s", name, best.name)
            return best
        raise ToolCallError(
            f"Symbol {name!r} is not available on this account "
            f"({len(records)} symbols returned)."
        )

    async def _raw_spot(self, info: Optional[SymbolInfo] = None) -> Any:
        info = info or self.symbol
        values: dict[str, Any] = {}
        if info is not None:
            values["symbol_id"] = info.symbol_id
            values["symbol_name"] = info.name
        return await self._call(TOOL_SPOT, values)

    async def get_quote(self) -> Quote:
        payload = await self._raw_spot()
        records = find_list(payload, "prices", "spots", "quotes")
        symbol_id = self.symbol.symbol_id if self.symbol else None
        record = None
        for candidate in records:
            if symbol_id is None or as_int(pick(candidate, "symbolId", "id"), None) in (None, symbol_id):
                record = candidate
                break
        if record is None:
            raise ToolCallError(f"No spot price returned for {self.active_name}")

        bid = self.price_scale.apply(as_float(pick(record, "bid", "bidPrice"), None))
        ask = self.price_scale.apply(as_float(pick(record, "ask", "askPrice"), None))
        if bid is None or ask is None:
            raise ToolCallError(f"Spot payload missing bid/ask: {record}")
        return Quote(
            symbol_id=symbol_id or -1,
            bid=bid,
            ask=ask,
            ts=parse_timestamp(pick(record, "timestamp", "time")),
        )

    async def get_balance(self) -> AccountSnapshot:
        """Always hits the wire - a cached balance must never size a position."""
        payload = await self._call(TOOL_BALANCE, {})
        record = payload if isinstance(payload, dict) else (find_list(payload) or [{}])[0]
        balance = self.money_scale.apply(
            as_float(pick(record, "balance", "accountBalance", "cash"), None)
        )
        if balance is None:
            raise ToolCallError(f"Balance payload had no usable balance field: {record}")
        equity = self.money_scale.apply(as_float(pick(record, "equity"), None))
        return AccountSnapshot(
            balance=balance,
            equity=equity,
            currency=str(pick(record, "currency", "depositCurrency", default="USD")),
            raw=record if isinstance(record, dict) else {},
        )

    # -- candles -----------------------------------------------------------

    async def get_candles(self, timeframe: str, count: int) -> list[Candle]:
        """Fetch OHLC bars, tolerating server-specific parameter requirements.

        The verified cTrader proxy advertises "(count) -> last N bars" in its own
        schema but actually rejects it with ``fromTimestamp: must not be null``,
        so we try the argument combinations in order of observed reliability
        rather than trusting the description text.
        """
        tool = self._conn.catalog.require(*TOOL_TRENDBARS)
        tf = get_timeframe(timeframe)
        props = {normalize(p) for p in tool.properties}
        has_from = any(normalize(a) in props for a in ("fromTimestamp", "from", "startTime", "start"))
        has_to = any(normalize(a) in props for a in ("toTimestamp", "to", "endTime", "end"))
        has_count = any(normalize(a) in props for a in ("count", "limit", "bars", "barCount"))

        base: dict[str, Any] = {
            "symbol_id": self.symbol.symbol_id if self.symbol else None,
            "symbol_name": self.active_name,
            "period": Candidates(tf.aliases),
        }

        candles: list[Candle] = []
        # Widening ladder: markets close overnight and at weekends, so a naive
        # count x period span can return far fewer bars than requested.
        for span_multiplier in (2.5, 6.0, 14.0):
            attempts: list[dict[str, Any]] = []
            now = datetime.now(tz=timezone.utc)
            span = min(tf.seconds * count * span_multiplier, 720 * 3600 - 60)
            start = now - timedelta(seconds=span)
            # `count` goes in alongside from/to: without it the server applies
            # its own default (100) and silently truncates the history, which
            # looks exactly like a closed market. Some builds reject the
            # combination, so the plain from/to form is kept as a fallback.
            if has_from and has_to and has_count:
                attempts.append({**base, "from_ts": _iso(start), "to_ts": _iso(now),
                                 "count": count})
            if has_from and has_to:
                attempts.append({**base, "from_ts": _iso(start), "to_ts": _iso(now)})
            if has_to and has_count:
                attempts.append({**base, "to_ts": _iso(now), "count": count})
            if has_count:
                attempts.append({**base, "count": count})
            if not attempts:
                attempts.append(base)

            for values in attempts:
                try:
                    payload = await self._call(TOOL_TRENDBARS, values)
                except (ToolCallError, ToolUnavailable) as exc:
                    log.debug("trendbars attempt %s failed: %s", sorted(values), str(exc)[:160])
                    continue
                got = self._to_candles(find_list(payload, "trendbars", "bars", "candles", "data"))
                # Keep the richest result: an attempt that succeeds but returns
                # a truncated series should not stop us trying a better shape.
                if len(got) > len(candles):
                    candles = got
                if len(candles) >= count * 0.9:
                    break
            if len(candles) >= count * 0.6:
                break

        if not candles:
            raise ToolCallError(f"No {timeframe} candles returned for {self.active_name}")
        if len(candles) < count * 0.5:
            self._warn_once(
                f"thin-{self.active_name}-{timeframe}",
                "Only %d %s bars available (asked for %d) - market may be closed.",
                len(candles), timeframe, count,
            )
        return candles[-count:]

    def _to_candles(self, rows: Sequence[Any]) -> list[Candle]:
        """Normalise both the flat OHLC shape and cTrader's delta encoding."""
        out: list[Candle] = []
        divisor = self.price_scale.divisor
        for row in rows:
            ts = parse_timestamp(
                pick(row, "timestamp", "utcTimestampInMinutes", "time", "t", "openTime", "barTime")
            )
            if ts is None:
                continue
            low_raw = as_float(pick(row, "low", "l", "lowPrice"), None)
            delta_open = as_float(pick(row, "deltaOpen"), None)
            if low_raw is not None and delta_open is not None:
                # cTrader relative encoding: everything is an offset from `low`.
                low = low_raw
                open_ = low + delta_open
                high = low + (as_float(pick(row, "deltaHigh"), 0.0) or 0.0)
                close = low + (as_float(pick(row, "deltaClose"), 0.0) or 0.0)
            else:
                open_ = as_float(pick(row, "open", "o", "openPrice"), None)
                high = as_float(pick(row, "high", "h", "highPrice"), None)
                low = low_raw
                close = as_float(pick(row, "close", "c", "closePrice"), None)
                if None in (open_, high, low, close):
                    continue
            out.append(
                Candle(
                    ts=ts,
                    open=open_ / divisor,
                    high=high / divisor,
                    low=low / divisor,
                    close=close / divisor,
                    volume=as_float(pick(row, "volume", "v", "tickVolume"), 0.0) or 0.0,
                )
            )
        # De-duplicate on open time and sort ascending; some servers page oddly.
        unique = {candle.ts: candle for candle in out}
        return [unique[ts] for ts in sorted(unique)]

    # -- account state -----------------------------------------------------

    async def get_positions(self) -> list[Position]:
        payload = await self._call(TOOL_POSITIONS, {}, strict=False)
        out: list[Position] = []
        for record in find_list(payload, "positions", "data"):
            out.append(
                Position(
                    position_id=str(pick(record, "positionId", "id", default="")),
                    symbol_id=as_int(pick(record, "symbolId"), None),
                    symbol_name=str(pick(record, "symbolName", "symbol", default="")),
                    side=normalize_side(pick(record, "tradeSide", "side", "direction")),
                    volume=self._volume_to_lots(as_float(pick(record, "volume", "lots", "quantity"), 0.0) or 0.0),
                    entry_price=self.price_scale.apply(
                        as_float(pick(record, "entryPrice", "openPrice", "price"), None)),
                    stop_loss=self.price_scale.apply(as_float(pick(record, "stopLoss", "sl"), None)),
                    take_profit=self.price_scale.apply(as_float(pick(record, "takeProfit", "tp"), None)),
                    label=str(pick(record, "label", "comment", default="")),
                    open_time=parse_timestamp(pick(record, "openTimestamp", "openTime", "timestamp")),
                    profit=self.money_scale.apply(
                        as_float(pick(record, "profit", "grossProfit", "netProfit", "pnl"), None)),
                    raw=record if isinstance(record, dict) else {},
                )
            )
        return out

    async def get_pending_orders(self) -> list[PendingOrder]:
        payload = await self._call(TOOL_PENDING, {}, strict=False)
        out: list[PendingOrder] = []
        for record in find_list(payload, "orders", "pendingOrders", "data"):
            out.append(
                PendingOrder(
                    order_id=str(pick(record, "orderId", "id", default="")),
                    symbol_id=as_int(pick(record, "symbolId"), None),
                    symbol_name=str(pick(record, "symbolName", "symbol", default="")),
                    side=normalize_side(pick(record, "tradeSide", "side", "direction")),
                    volume=self._volume_to_lots(as_float(pick(record, "volume", "lots", "quantity"), 0.0) or 0.0),
                    price=self.price_scale.apply(
                        as_float(pick(record, "limitPrice", "price", "stopPrice"), None)),
                    order_type=str(pick(record, "orderType", "type", default="")),
                    stop_loss=self.price_scale.apply(as_float(pick(record, "stopLoss", "sl"), None)),
                    take_profit=self.price_scale.apply(as_float(pick(record, "takeProfit", "tp"), None)),
                    label=str(pick(record, "label", "comment", default="")),
                    created_time=parse_timestamp(pick(record, "timestamp", "createdTime", "openTime")),
                    raw=record if isinstance(record, dict) else {},
                )
            )
        return out

    async def get_deals(self, since: datetime, until: Optional[datetime] = None) -> list[Deal]:
        """Closed-trade history - the source of truth for realised daily P&L."""
        until = until or datetime.now(tz=timezone.utc)
        values = {"from_ts": _iso(since), "to_ts": _iso(until), "symbol_id":
                  self.symbol.symbol_id if self.symbol else None}
        payload = await self._call(TOOL_DEALS, values, strict=False)
        out: list[Deal] = []
        for record in find_list(payload, "deals", "data"):
            gross = as_float(pick(record, "grossProfit", "profit", "pnl"), None)
            net = as_float(pick(record, "netProfit", "net"), None)
            commission = as_float(pick(record, "commission"), 0.0) or 0.0
            swap = as_float(pick(record, "swap"), 0.0) or 0.0
            raw_profit = net if net is not None else gross
            if raw_profit is None:
                continue  # an opening deal carries no P&L; ignore it
            profit = self.money_scale.apply(raw_profit) or 0.0
            if net is None:
                # Only fold in costs when the server gave us the gross figure.
                profit += (self.money_scale.apply(commission) or 0.0)
                profit += (self.money_scale.apply(swap) or 0.0)
            out.append(
                Deal(
                    deal_id=str(pick(record, "dealId", "id", default="")),
                    symbol_id=as_int(pick(record, "symbolId"), None),
                    position_id=str(pick(record, "positionId", default="")) or None,
                    volume=self._volume_to_lots(as_float(pick(record, "volume", "quantity"), 0.0) or 0.0),
                    price=self.price_scale.apply(as_float(pick(record, "executionPrice", "price"), None)),
                    profit=profit,
                    commission=self.money_scale.apply(commission) or 0.0,
                    swap=self.money_scale.apply(swap) or 0.0,
                    executed_at=parse_timestamp(
                        pick(record, "executionTimestamp", "timestamp", "time", "executionTime")),
                    label=str(pick(record, "label", "comment", default="")),
                    raw=record if isinstance(record, dict) else {},
                )
            )
        return out

    # -- volume conversion -------------------------------------------------

    def _volume_property(self) -> tuple[Optional[str], dict[str, Any]]:
        tool = self._tool(TOOL_CREATE_ORDER)
        if tool is None:
            return None, {}
        from mcp_client.schema import map_properties
        for prop, canonical in map_properties(tool).items():
            if canonical == "volume":
                return prop, tool.properties.get(prop, {})
        return None, {}

    def resolve_volume_mode(self) -> str:
        """Decide whether the server wants lots, units, or cTrader centi-units.

        Getting this wrong is a 100x-10000x position size error, so an ambiguous
        schema deliberately falls back to the explicit config value and shouts
        about it in the log.
        """
        if self._cfg.volume_unit_mode != "auto":
            return self._cfg.volume_unit_mode
        prop, schema = self._volume_property()
        if prop is None:
            return "lots"

        text = f"{prop} {schema.get('description', '')}".lower()
        verdict = wants_units(prop, schema)
        is_integer = "integer" in declared_types(schema)

        if verdict is False and not is_integer:
            mode = "lots"
        elif verdict is True:
            mode = "centiunits" if ("cent" in text or "1/100" in text or is_integer) else "units"
        elif is_integer:
            # Decisive: lot sizes are fractional (0.01 steps), so a field the
            # schema declares as an integer cannot be carrying lots. cTrader's
            # own convention for an integer volume is 1/100 of a unit, so 0.10
            # lots of a 1-unit contract is 10, not 0. Without this the value
            # rounds to zero and every order is rejected.
            mode = "centiunits"
            self._warn_once(
                "volume-mode",
                "create_order.%s is declared %s - lots cannot be integers, so volume "
                "is being sent as cTrader centi-units (lots x contract_size x 100). "
                "Override with VOLUME_UNIT_MODE if your server differs.",
                prop, declared_types(schema) or "integer",
            )
        else:
            mode = "lots"
            self._warn_once(
                "volume-mode",
                "create_order volume field %r does not say lots or units; assuming LOTS. "
                "Set VOLUME_UNIT_MODE explicitly before trading live. Schema: %s",
                prop, schema,
            )
        return mode

    def lots_to_wire(self, lots: float) -> float:
        mode = self.resolve_volume_mode()
        contract = self.profile.contract_size
        if mode == "units":
            return lots * contract
        if mode == "centiunits":
            return lots * contract * 100.0
        return lots

    def _volume_to_lots(self, wire_volume: float) -> float:
        mode = self.resolve_volume_mode()
        contract = self.profile.contract_size
        if mode == "units" and contract:
            return wire_volume / contract
        if mode == "centiunits" and contract:
            return wire_volume / (contract * 100.0)
        return wire_volume

    def _price_field_mode(self, canonical: str) -> str:
        """Does the SL/TP field want an absolute price, pips, or points?"""
        tool = self._tool(TOOL_CREATE_ORDER)
        if tool is None:
            return "price"
        from mcp_client.schema import map_properties
        for prop, mapped in map_properties(tool).items():
            if mapped != canonical:
                continue
            text = f"{prop} {tool.properties.get(prop, {}).get('description', '')}".lower()
            if "pip" in text:
                return "pips"
            if "point" in text:
                return "points"
            return "price"
        return "price"

    def _relative_distance(self, canonical: str, absolute: Optional[float],
                           reference: Optional[float]) -> Optional[float]:
        """Convert an absolute SL/TP into the units the schema actually wants."""
        if absolute is None or reference is None:
            return absolute
        mode = self._price_field_mode(canonical)
        if mode == "price":
            return absolute
        distance_points = abs(absolute - reference) / self.point_size
        return distance_points / 10.0 if mode == "pips" else distance_points

    # -- order actions -----------------------------------------------------

    def describe_order_payload(self, request: OrderRequest) -> dict[str, Any]:
        """Build the exact ``create_order`` arguments (also used for paper mode)."""
        tool = self._conn.catalog.require(*TOOL_CREATE_ORDER)
        reference = request.price
        values: dict[str, Any] = {
            "symbol_id": self.symbol.symbol_id if self.symbol else None,
            "symbol_name": self.symbol.name if self.symbol else self.active_name,
            "order_type": ORDER_TYPE_CANDIDATES.get(request.order_type,
                                                    Candidates.of(request.order_type)),
            "trade_side": SIDE_CANDIDATES.get(request.side, Candidates.of(request.side)),
            "volume": self.lots_to_wire(request.volume_lots),
            "limit_price": request.price,
            "stop_loss": self._relative_distance("stop_loss", request.stop_loss, reference),
            "take_profit": self._relative_distance("take_profit", request.take_profit, reference),
            "label": request.label,
            "comment": request.comment or request.label,
        }
        if request.expiry is not None:
            # Servers differ: some want epoch milliseconds (integer), others an
            # ISO string. Offer both and let the schema pick; if neither fits,
            # the binder drops the optional field rather than sending a value
            # the server will reject.
            values["expiry"] = Candidates.of(
                int(request.expiry.timestamp() * 1000), _iso(request.expiry)
            )
            # An expiring order must say so, or the broker treats it as GTC.
            values["time_in_force"] = Candidates.of("GOOD_TILL_DATE", "GTD")

        arguments = bind_arguments(tool, values, extras=self._cfg_extra_order_fields())
        # Never leave a dangling time-in-force if the expiry itself was dropped.
        tif_prop = self._property_for("time_in_force")
        expiry_prop = self._property_for("expiry")
        if tif_prop and expiry_prop and tif_prop in arguments and expiry_prop not in arguments:
            arguments.pop(tif_prop)
        return arguments

    def _property_for(self, canonical: str) -> Optional[str]:
        """Upstream property name that carries ``canonical`` on create_order."""
        tool = self._tool(TOOL_CREATE_ORDER)
        if tool is None:
            return None
        from mcp_client.schema import map_properties
        for prop, mapped in map_properties(tool).items():
            if mapped == canonical:
                return prop
        return None

    def _cfg_extra_order_fields(self) -> dict[str, Any]:
        return dict(getattr(self._cfg, "extra_order_fields", {}) or {})

    async def create_order(self, request: OrderRequest) -> Any:
        tool = self._conn.catalog.require(*TOOL_CREATE_ORDER)
        arguments = self.describe_order_payload(request)
        log.info("Submitting %s -> %s", tool.name, arguments)
        result = await self._conn.call_tool(tool.name, arguments,
                                            timeout=self._cfg.mcp_call_timeout)
        return extract_payload(result)

    async def cancel_order(self, order_id: str) -> Any:
        return await self._call(TOOL_CANCEL_ORDER, {"order_id": order_id})

    async def close_position(self, position_id: str,
                             volume_lots: Optional[float] = None) -> Any:
        """Close a position. ``volume`` is required by the live schema, so the
        caller must say how much - a partial close needs it and a full close
        cannot omit it."""
        values: dict[str, Any] = {"position_id": position_id}
        if volume_lots is not None:
            values["volume"] = self.lots_to_wire(volume_lots)
        return await self._call(TOOL_CLOSE_POSITION, values)

    async def get_version(self) -> Any:
        if not self.has(TOOL_VERSION):
            return None
        return await self._call(TOOL_VERSION, {}, strict=False)


def _iso(moment: datetime) -> str:
    """UTC ISO-8601 with a trailing Z, the form the cTrader proxy accepts."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
