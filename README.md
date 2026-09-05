# ICT Silver Bullet Bot — XAUUSD

A 24/7 automated trading bot that executes the ICT **Silver Bullet** setup on gold
(XAUUSD) through the cTrader MCP server, built for one-command deployment on
Railway.app.

> ### ⚠️ This bot places live orders
> It is not signal-only. Before pointing it at a funded account, confirm the
> connected cTrader account is a **demo** one. You do not have to take this on
> trust: the Bearer token encodes the environment, and the bot decodes and logs
> it on every start-up —
>
> ```
> INFO | main | cTrader account environment=DEMO plant=icmarkets
> ```
>
> Two independent switches must both be thrown before a real order can reach a
> live account: `TRADING_MODE=live` **and** `ALLOW_LIVE_ENVIRONMENT=true`. The
> defaults (`paper`, `false`) are the safe ones, and an undecodable token is
> treated as live.

---

## Quick start

```bash
cp .env.example .env          # fill in CTRADER_MCP_TOKEN + Telegram values
pip install -r requirements-dev.txt
pytest                        # 35 tests, no network required
python main.py                # starts in paper mode
```

### Railway

1. Create a new project from this repository. The `Dockerfile` is picked up
   automatically (`railway.json` pins it).
2. Set the variables from `.env.example` under **Variables**. At minimum:
   `CTRADER_MCP_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Leave `TRADING_MODE=paper` for the first session and watch the logs.
4. Deploy as a **worker** — the bot is a long-running scheduler, not an HTTP
   service. That is the single process type declared in the `Procfile`. If you
   deploy it as a web service instead, Railway sets `PORT` and the bot
   automatically serves `/health` and `/status` on it.

> **Note on the `Procfile`:** the format has no comment syntax. Every non-empty
> line is parsed as `<process type>: <command>`, split on the first colon, so a
> `#`-prefixed line containing a colon is still read as a process definition and
> Railway will create a service that tries to execute the prose. Keep the file to
> its one real line.

---

## Architecture

```
main.py                  boot, signal handling, environment announcement
healthcheck.py           container probe used by the Dockerfile HEALTHCHECK
config.py                every tunable, validated at start-up
models.py                Candle / Quote / Position / Deal (already unscaled)
symbols.py               per-instrument profiles + weekday/weekend calendar

mcp_client/
  transport.py           supervised Streamable HTTP session + reconnection
  schema.py              live tools/list discovery, dynamic argument binding
  ctrader.py             typed cTrader operations built from the live schemas
  parsing.py             tolerant result parsing (structured or text payloads)
  token_info.py          Bearer-token introspection (demo vs live)
  errors.py              typed failures the engine reacts to differently

strategy/
  liquidity.py           liquidity pools, sweep detection, draw-on-liquidity
  structure.py           displacement + market structure shift
  fvg.py                 fair value gaps and mitigation
  silver_bullet.py       the five-step pipeline
  models.py              Sweep / MSS / FVG / TradeSetup

risk/
  sizing.py              1% risk sized off the structural stop
  daily.py               daily drawdown from the broker's own deals
  guards.py              spread, exposure, environment, kill-switch gates

engine/
  orchestrator.py        the 24/7 control loop
  executor.py            the only code path that can create an order
  monitor.py             fill / TP / SL detection and stale-order cleanup
  state.py               status snapshot + best-effort persistence

utils/                   logging (with secret redaction), sessions, prices,
                         timeframes, Telegram, health endpoint
```

---

## MCP integration

**Endpoint** (hardcoded, deliberately not configurable — a typo in an env var
must never be able to point live order flow at another host):

```
https://mcp.ctrader.com/trading/mcp
```

**Transport:** Streamable HTTP via the official `mcp` Python SDK — JSON-RPC 2.0
over `POST`, with responses arriving as either JSON or `text/event-stream`.
Not an SSE-only client.

**Auth:** `Authorization: Bearer {CTRADER_MCP_TOKEN}`, header only, never in the
URL, never split into host + token.

**Session:** the real upstream mints the session. The bot sends `initialize`,
the server returns `Mcp-Session-Id`, and every subsequent request echoes that
exact header. The handshake is never faked locally — a fabricated session id
breaks continuity and every later call is rejected.

**Discovery:** `tools/list` runs on every connect and the full tool set is logged
with its schemas. No tool name or parameter name is hardcoded blind. Note there
is **no `place_limit_order`** — the correct entry point for a new order (market
or limit) is **`create_order`**, and its arguments are assembled from the live
`inputSchema` at call time.

### Verified against the live server

These were confirmed by calling a running cTrader MCP proxy (`rest-proxy`
v1.0.18), not assumed, and the client is built around them:

| Behaviour | Finding |
|---|---|
| Price encoding | Integers scaled by **1e5** — XAUUSD 4431.23 arrives as `443123000` |
| Money encoding | Cents (**÷100**) |
| Trendbar shape | Flat `{timestamp, open, high, low, close, volume}`, timestamps in **epoch ms** |
| Trendbar periods | `M_1`, `M_5`, `M_15`, `M_30`, `H_1`, `H_4`, `D_1`, `W_1`, `MN_1` — underscored |
| `get_trendbars` args | The schema advertises "(count) → last N bars", but the server **rejects it** with `fromTimestamp: must not be null` |
| `get_spot_prices` | Takes `symbolId` as an **array** of integers |
| Symbol listing | `symbolId`/`symbolName`/`description` only — no `digits` or `lotSize` |
| `create_order.volume` | Declared **`integer`** — so it is not lots. cTrader carries volume as 1/100 of a unit, so 0.10 BTC is `10` and 0.08 lots of gold is `800` |
| `create_order` expiry | `expirationTimestamp` is an **integer** (epoch ms), paired with `timeInForce=GOOD_TILL_DATE` |
| Order/position ids | Integers, not strings |

Two of these shaped the design directly:

* Scaling is **auto-detected** from the live feed against a plausibility band and
  logged (`Price scale: divide by 100000 (detected)`). Override with
  `PRICE_SCALE` / `MONEY_SCALE` if a server build differs. Getting this wrong is
  not a cosmetic bug — it is a 100,000× position-sizing error.
* Because a tool's declared schema and its actual validation can disagree,
  candle fetching walks a ladder of argument combinations and keeps the richest
  result. `count` is always sent alongside `from`/`to` where the schema allows
  it: without it the server applies its own default of 100 and silently
  truncates the history, which looks exactly like a closed market.
* An integer-typed `volume` is decisive evidence that the field is not carrying
  lots — lot sizes are fractional. Reading it as lots makes `int(round(0.10))`
  zero and every order is rejected, so the bot infers cTrader centi-units and
  says so in the log.
* A value the schema cannot represent is **dropped**, not sent: an ISO timestamp
  bound into an `integer` field would get the whole order rejected.

### Resilience

* Every upstream call has an explicit timeout (default 15s) — a hung request can
  never stall the bot; a timeout forces a session rebuild.
* A supervisor task owns the connection lifecycle and reconnects with jittered
  exponential backoff. The container never crash-loops.
* Failures are classified as **auth** / **network** / **other**, because the
  responses differ: auth pages the operator and parks the bot for minutes, a
  network blip retries in seconds. A proxy refusal that surfaces as a `403` is
  correctly treated as network, not as a bad token.
* `ExceptionGroup`s are unwrapped before logging, so you get
  `ProxyError: 403 Forbidden` instead of
  `unhandled errors in a TaskGroup (1 sub-exception)`.
* A 401 sends an emergency Telegram alert, pauses execution, and retries with a
  long backoff.

---

## Instruments and the trading calendar

| Day | Instrument | Why |
|---|---|---|
| Mon–Fri | `XAUUSD` | Gold trades the FX week. |
| Sat–Sun | `BTCUSD` | Gold is shut from Friday evening to Sunday evening; crypto runs 24/7. |

Set `WEEKEND_TRADING=false` to idle at weekends instead. The instrument is
chosen from the **local (Europe/Tirane) date**, so a Saturday window is a
Saturday window regardless of what UTC thinks.

Every threshold in this bot is expressed in **points**, and a point means very
different things per market. Measured on the live feed:

| | XAUUSD | BTCUSD |
|---|---|---|
| Price | ~4,431 | ~80,067 |
| Median M1 body | 35 points | 2,960 points |
| Live spread | 40 points | 500 points |
| Contract size | 100 oz/lot | 1 BTC/lot |

So gold's 35-point spread cap would reject **every** BTC trade, and gold's
contract size would size a BTC position 100× too large. Each instrument
therefore carries its own `SymbolProfile` (`symbols.py`) holding contract
mechanics and every point-based threshold. The BTC values were derived from bars
pulled off the live server, keeping the same *relative* meaning each has for
gold.

Overrides use the `<SYMBOL>_<FIELD>` form — `BTCUSD_MAX_SPREAD_POINTS=900`. The
legacy global variables (`MAX_SPREAD_POINTS`, `CONTRACT_SIZE`, …) still work but
apply **only to the weekday instrument**, so an existing deployment keeps its
gold configuration without leaking it onto crypto.

> **Confirm `CONTRACT_SIZE` per symbol.** It is the one input the API does not
> publish — the symbol listing carries no `lotSize` — so the bot prints the
> value in use at start-up (`profile | BTCUSD: … contract=1.0 …`). Check it
> against your cTrader symbol specification before trading a new instrument.

## The strategy

Analysis runs **only** inside three one-hour windows, in Albania local time via
`ZoneInfo("Europe/Tirane")` — a real timezone, not a fixed offset, so CET/CEST
transitions are handled automatically:

| Window | Local (Europe/Tirane) | ICT equivalent |
|---|---|---|
| Morning | 09:00 – 10:00 | London open Silver Bullet |
| Afternoon | 16:00 – 17:00 | New York AM (10:00–11:00 ET) |
| Evening | 20:00 – 21:00 | New York PM (14:00–15:00 ET) |

All five steps must pass, in order, or no trade is taken.

**1 — Liquidity sweep.** Pools are built from Asian/London/NY session highs and
lows, previous-day extremes (PDH/PDL), equal highs/lows and recent swing points.
A sweep counts only if price takes the level *and closes back through it* within
a few bars — a level taken and held is a breakout, and fading it is how accounts
die. Levels that were already traded through earlier in the day are dropped: once
price runs a high, the stops behind it are gone and the level is spent.

**2 — Displacement & MSS.** The raid must be followed by a candle body well above
the recent average that closes through the opposing short-term structure.

**3 — Fair Value Gap.** The 3-candle imbalance inside the displacement leg, on M1
(falling back to M5, since a fast M1 leg often leaves no qualifying gap). Gaps
already more than half mitigated are discarded.

**4 — Entry.** A **limit** order at the FVG edge — never a market chase. Default
`FVG_ENTRY_MODE=proximal` (the edge price touches first); `mid` (consequent
encroachment) and `distal` are also available.

**5 — Stops and targets.**
* **SL** is structural: 20 points (2 pips) beyond the extreme wick of the whole
  sweep + displacement structure. Never a fixed pip count.
* **TP** is the nearest draw on liquidity — session high/low, equal highs/lows,
  PDH/PDL, or an unfilled HTF FVG — that still pays at least **1:2**. If nothing
  clears that floor, the setup is refused rather than downgraded.

When a sweep candidate fails any step, the bot moves on to the next candidate and
records *why* the first was rejected, so a quiet window still explains itself:

```
[SB-20260904-AFTERNOON] SETUP: Swept LONDON_HIGH @ 4430.00 (80pt raid, 14:02 UTC)
  | MSS through 4422.00 @ 14:04 (1230pt displacement)
  | Bearish FVG 4419.50-4425.00 (550pt)
  | Entry 4419.50 (proximal edge) | SL 4431.00 (1150pt) | TP 4392.00 -> PDL
  | Risk/Reward 2.39R
```

---

## Risk management

| Control | Behaviour |
|---|---|
| **Position sizing** | Exactly `RISK_PER_TRADE_PCT` (1.0%) of the **live** balance, divided by the actual structural stop distance. `get_balance` is called before every sizing calculation — a cached balance never sizes a position. Lots round **down** onto the volume step; rounding up would breach the limit on every trade. |
| **Below the minimum lot** | The trade is **refused**, not rounded up. If 1% risk implies 0.004 lots, taking the 0.01 minimum would risk 2.5×the limit. |
| **Daily drawdown** | Trading halts for the rest of the day at `DAILY_MAX_DRAWDOWN_PCT` (3.0%). Realised P&L is recomputed from the broker's own `get_deals` on every pass, never tallied in memory — a redeploy must not let the bot forget it is already down. Floating P&L on open positions is included by default. |
| **One trade per window** | Enforced by writing a window label (`SB-BTCUSD-20260905-AFTERNOON`) onto every order and reading it back off `get_positions`/`get_pending_orders`. In-memory state resets on redeploy; the broker's order book does not. |
| **Notional ceiling** | A position whose value exceeds `MAX_NOTIONAL_LEVERAGE` × balance is refused. This is a backstop against a *price-scale* failure: if auto-detection ever picked the wrong divisor, entry price arrives as 8,006,716,000 instead of 80,067 and every other number still looks reasonable. |
| **Spread guard** | Entry is blocked above the active profile's `max_spread_points` (35 for gold, 700 for BTC), checked from `get_spot_prices` — and re-checked immediately before submission, because a minute can pass between analysis and execution. |
| **Kill switch** | `KILL_SWITCH=true` blocks all new orders without a redeploy. |
| **Stale orders** | Unfilled limits are cancelled once their window closes — an FVG entry that was not taken inside its window has lost its premise. |

---

## Telegram notifications

Order entries (with the full setup narrative), fills, TP/SL hits with realised
P&L, cancellations, sizing refusals, drawdown halts, and connection/auth errors.
Alerts are de-duplicated over a short window so an outage cannot flood the chat.

The notifier talks to the Bot API directly with `httpx` rather than pulling in
`python-telegram-bot`: the bot only ever sends one-way messages, so the full
framework would add a polling/Updater stack and its dependency tree to the image
for what is a single `POST`. If Telegram is unconfigured or failing, alerts fall
back to the log — a dead notifier is never a reason to stop managing live risk.

---

## Observability

On every connect the bot runs a **preflight**: it builds one real `create_order`
payload per instrument at the minimum permitted size and logs it *without
sending it*.

```
PREFLIGHT BTCUSD (NOT sent) | min 0.01 lots -> wire volume 1 | {'symbolId': 10026,
  'orderType': 'LIMIT', 'tradeSide': 'SELL', 'volume': 1, 'limitPrice': 79478.75, ...}
```

The order payload is assembled from the live schema, so a mistake in it would
otherwise only surface at the exact moment a setup appears — the worst possible
time to learn that volume rounds to zero. The preflight turns that unknown into a
log line at boot, and raises an alert if volume resolves to `0`. It proves the
payload is well-formed and shows the real numbers; only a live order proves the
broker accepts it.

`GET /status` (when `PORT` is set) returns connection state, session id, tool
list, call/error/timeout counters, last error, executed windows, and the most
recent rejection reasons. `GET /health` returns 503 while the MCP session is
down, so Railway can act on it.

The Bearer token and Telegram token are registered with a logging filter and are
redacted from all log output.

---

## Configuration

Every variable is documented in [`.env.example`](.env.example). The ones worth
reading before going live:

| Variable | Default | Why it matters |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` runs the entire pipeline, including schema binding, and logs the exact payload it *would* have sent. |
| `ALLOW_LIVE_ENVIRONMENT` | `false` | Second switch required for a live (non-demo) account. |
| `VOLUME_UNIT_MODE` | `auto` | Reads the live `create_order` schema to decide lots vs units vs cTrader centi-units. **Set it explicitly before trading live** — guessing wrong is a 100× size error, and the bot logs a warning when the schema is ambiguous. |
| `PRICE_SCALE` / `MONEY_SCALE` | `auto` | Auto-detected and logged; override only if the numbers look wrong. |
| `EXTRA_ORDER_FIELDS` | — | JSON of literal fields merged into `create_order`, for a server build that requires a parameter this bot does not know about. No code change needed. |
| `MAX_SPREAD_POINTS` | `35` | Note: gold spreads on IC Markets were observed at ~40 points outside peak liquidity, so this guard will legitimately block some windows. Raise it deliberately, not reflexively. |

---

## Testing

```bash
pytest            # 54 tests: strategy, risk, transport, instruments, integration
```

The integration tests run the entire bot — gateway, strategy, guards, sizing,
executor — against an in-process fake MCP server whose tool schemas are the ones
captured from the real cTrader endpoint, with payloads in the real wire format
(1e5-scaled prices, money in cents, epoch-ms timestamps). They cover paper vs
live behaviour, the one-trade-per-window lock across ticks and across a simulated
redeploy, the spread and drawdown halts, and stale-order cleanup.

---

## Operational notes

* The bot is a scheduler, not a web service; deploy it as a `worker`.
* Analysis is skipped entirely outside the three windows — no candle downloads,
  no wasted calls.
* Symbol resolution and price/money scaling are re-derived after every reconnect,
  because they must match the session actually in use.
* State in `STATE_DIR` is a convenience for avoiding duplicate notifications
  across a redeploy. It is never trusted for anything that affects risk.
