"""An in-process stand-in for the cTrader MCP server.

The tool schemas below are the ones captured from the *real* server
(``get_trendbars``, ``get_spot_prices``, ``get_symbols``), plus a plausible
``create_order``.  Payloads use the real wire conventions too - prices as
integers scaled by 1e5, money in cents, timestamps in epoch milliseconds - so
the gateway's scaling and parsing are exercised exactly as they will be live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mcp_client.schema import ToolCatalog, ToolSpec
from mcp_client.transport import ConnectionState, ConnectionStats
from tests import fixtures

PRICE_SCALE = 100_000        # cTrader relative prices
MONEY_SCALE = 100            # cTrader money in cents


def _p(price: float) -> int:
    return int(round(price * PRICE_SCALE))


TOOLS = [
    ToolSpec("get_version", "Get service version", {"type": "object", "properties": {}}),
    ToolSpec("get_symbols", "Get available trading symbols", {"type": "object", "properties": {}}),
    ToolSpec("get_balance", "Get account balance", {"type": "object", "properties": {}}),
    ToolSpec("get_spot_prices", "Get current bid/ask prices for symbols", {
        "type": "object",
        "properties": {"symbolId": {"type": "array", "items": {"type": "integer"}}},
        "required": ["symbolId"],
    }),
    ToolSpec("get_trendbars", "Get historical OHLCV candle data for a symbol.", {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "Max bars to return (default 100)."},
            "fromTimestamp": {"type": "string", "description": "Start time (epoch ms or ISO-8601)."},
            "period": {"type": "string",
                       "enum": ["M_1", "M_5", "M_15", "M_30", "H_1", "H_4", "D_1", "W_1", "MN_1"]},
            "symbolId": {"type": "integer"},
            "toTimestamp": {"type": "string"},
        },
        "required": ["symbolId", "period"],
    }),
    ToolSpec("get_positions", "Get open positions", {"type": "object", "properties": {}}),
    ToolSpec("get_pending_orders", "Get pending orders", {"type": "object", "properties": {}}),
    ToolSpec("get_deals", "Get deal history", {
        "type": "object",
        "properties": {"fromTimestamp": {"type": "string"}, "toTimestamp": {"type": "string"}},
        "required": ["fromTimestamp", "toTimestamp"],
    }),
    # Verbatim from the live server's tools/list (ctrader-trading v0.4.0).
    # `volume` really is an integer: cTrader carries it as 1/100 of a unit.
    ToolSpec("create_order", "Create an order", {
        "type": "object",
        "properties": {
            "symbolId": {"type": "integer"},
            "orderType": {"type": "string",
                          "enum": ["MARKET", "LIMIT", "STOP", "MARKET_RANGE", "STOP_LIMIT"]},
            "tradeSide": {"type": "string", "enum": ["BUY", "SELL"]},
            "volume": {"type": "integer"},
            "limitPrice": {"type": "number"},
            "stopPrice": {"type": "number"},
            # The descriptions matter: they mention pips in order to point the
            # reader at the relative fields, which is exactly what fooled an
            # earlier description-based heuristic into sending a distance as a price.
            "stopLoss": {"type": "number",
                         "description": "Stop loss price. For pips use relativeStopLoss."},
            "takeProfit": {"type": "number",
                           "description": "Take profit price. For pips use relativeTakeProfit."},
            "relativeStopLoss": {"type": "integer", "description": "Relative stop loss in pips"},
            "relativeTakeProfit": {"type": "integer",
                                   "description": "Relative take profit in pips"},
            "comment": {"type": "string"},
            "label": {"type": "string"},
            "timeInForce": {"type": "string",
                            "enum": ["GOOD_TILL_CANCEL", "GOOD_TILL_DATE", "IMMEDIATE_OR_CANCEL"]},
            "baseSlippagePrice": {"type": "number"},
            "slippageInPoints": {"type": "integer"},
            "expirationTimestamp": {"type": "integer"},
        },
        "required": ["symbolId", "orderType", "tradeSide", "volume"],
    }),
    ToolSpec("cancel_order", "Cancel a pending order", {
        "type": "object",
        "properties": {"orderId": {"type": "integer"}}, "required": ["orderId"],
    }),
    ToolSpec("close_position", "Close an open position", {
        "type": "object",
        "properties": {"positionId": {"type": "integer"}, "volume": {"type": "integer"}},
        "required": ["positionId", "volume"],
    }),
]


@dataclass
class FakeResult:
    """Mimics mcp's CallToolResult (2.x snake_case field names)."""

    structured_content: Any
    content: list = field(default_factory=list)
    is_error: bool = False


class FakeConnection:
    """Drop-in replacement for :class:`MCPConnection` in tests."""

    def __init__(self, *, balance: float = 10_000.0, positions=None, pending=None,
                 deals=None, tools: Optional[list[ToolSpec]] = None) -> None:
        self.catalog = ToolCatalog(tools if tools is not None else TOOLS)
        self.state = ConnectionState.READY
        self.is_ready = True
        self.stats = ConnectionStats(connects=1, session_id="fake-session-id")
        self.balance = balance
        self.positions = positions or []
        self.pending = pending or []
        self.deals = deals or []
        self.calls: list[tuple[str, dict]] = []
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.fail_with: dict[str, Exception] = {}

    def snapshot(self) -> dict:
        return {"state": self.state.value, "tools": self.catalog.names}

    def request_reconnect(self, reason: str) -> None:  # pragma: no cover - unused here
        pass

    async def call_tool(self, name: str, arguments: dict, *, timeout=None) -> FakeResult:
        self.calls.append((name, arguments))
        if name in self.fail_with:
            raise self.fail_with[name]
        handler = getattr(self, f"_{name}")
        return FakeResult(structured_content=handler(arguments))

    # -- tool implementations ---------------------------------------------

    def _get_version(self, _args: dict) -> dict:
        return {"version": "1.0.18", "service": "rest-proxy"}

    def _get_symbols(self, _args: dict) -> dict:
        return {"symbols": [
            {"symbolId": 1, "symbolName": "EURUSD", "enabled": True,
             "description": "Euro vs US Dollar"},
            {"symbolId": 41, "symbolName": "XAUUSD", "enabled": True,
             "description": "Gold vs US Dollar"},
            {"symbolId": 10026, "symbolName": "BTCUSD", "enabled": True,
             "description": "Bitcoin"},
        ]}

    def _get_balance(self, _args: dict) -> dict:
        return {"balance": int(self.balance * MONEY_SCALE),
                "equity": int(self.balance * MONEY_SCALE), "currency": "USD"}

    def _get_spot_prices(self, args: dict) -> dict:
        requested = args.get("symbolId") or [41]
        if not isinstance(requested, list):
            requested = [requested]
        quotes = {41: fixtures.quote(), 10026: fixtures.btc_quote()}
        prices = []
        for symbol_id in requested:
            quote = quotes.get(symbol_id)
            if quote is None:
                continue
            prices.append({"symbolId": symbol_id, "bid": _p(quote.bid), "ask": _p(quote.ask),
                           "timestamp": int((quote.ts or fixtures.NOW).timestamp() * 1000)})
        return {"prices": prices}

    def _get_trendbars(self, args: dict) -> dict:
        by_symbol = {
            41: {"M_1": fixtures.m1_series, "M_5": fixtures.m5_history,
                 "H_1": fixtures.h1_history},
            10026: {"M_1": fixtures.btc_m1_series, "M_5": fixtures.btc_m5_history,
                    "H_1": fixtures.btc_h1_history},
        }
        builder = by_symbol.get(args.get("symbolId"), {}).get(args.get("period"))
        series = builder() if builder else []
        return {
            "symbolId": args.get("symbolId"),
            "period": args.get("period"),
            "trendbars": [
                {"timestamp": int(c.ts.timestamp() * 1000), "open": _p(c.open),
                 "high": _p(c.high), "low": _p(c.low), "close": _p(c.close),
                 "volume": int(c.volume)}
                for c in series
            ],
        }

    def _get_positions(self, _args: dict) -> dict:
        return {"positions": self.positions}

    def _get_pending_orders(self, _args: dict) -> dict:
        return {"orders": self.pending}

    def _get_deals(self, _args: dict) -> dict:
        return {"deals": self.deals}

    def _create_order(self, args: dict) -> dict:
        self.orders.append(args)
        return {"orderId": f"ord-{len(self.orders)}", "status": "ACCEPTED"}

    def _cancel_order(self, args: dict) -> dict:
        self.cancelled.append(str(args.get("orderId", "")))
        return {"status": "CANCELLED"}

    def _close_position(self, args: dict) -> dict:
        return {"status": "CLOSED", "positionId": args.get("positionId")}
