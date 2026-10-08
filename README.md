# Signal — three-layer AI trading app

An initial, functional **paper-trading** vertical slice built around the three requested projects.

The Paper Trading dashboard at `/paper` persists its simulated account, positions,
closed trades, watchlist, and journal in `data/signal.db`. It starts with 100,000
USDT; set `PAPER_STARTING_CASH` before the database is first created to choose a
different practice balance. This feature never sends orders to a broker or venue.
The product model is market-agnostic: every run selects a market, venue, account mode and symbol.

## Start the app on macOS

Signal now includes a local installable web app. It serves the interface and the Python
pipeline together, so results are never replaced with browser-side sample data.

The interface follows a staged workflow instead of placing everything on one screen:

1. Choose a market.
2. Explore its searchable universe, breadth, top gainers, top losers and the clearly
   labelled Signal Fear & Greed calculation.
3. Open AI Scores to rank the available assets by trend, mood, liquidity and risk.
4. Open one asset for an immediate, deterministic Quick Signal, then optionally ask
   for its Fast AI Explanation. Start the slower second opinion only with
   **Advanced Deep Research Report**.

```bash
cd ~/Downloads/ai-trading-bot
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,upstreams]'
cp .env.example .env
open -a TextEdit .env
signal-app
```

Add one supported LLM provider key to `.env`. Start OpenBB and Paperclip locally and
set their URLs in the same file. Signal opens at `http://127.0.0.1:8787` and its
system-check panel shows which layers still need setup. Chrome can install that page
as a standalone app.

The Analyse page separates the two pipelines:

1. `POST /api/analyze/quick` obtains a live OpenBB or WEEX snapshot and applies local
   momentum, volatility, liquidity, funding and risk rules. It never loads
   TradingAgents or calls an LLM.
2. `POST /api/analyze/summary` directly calls the configured LLM with a compact prompt
   to explain (and never replace) the Quick Signal. It falls back to deterministic
   reasoning after its 15-second deadline.
3. `POST /api/analyze/deep` runs `TradingAgentsGraph.propagate()` only after explicit
   user action. Successful results are cached for 20 minutes by market and symbol;
   send `refresh: true` to deliberately replace one.
4. AI failures are displayed separately, leaving the Quick Signal and market data on
   screen. Paperclip receives completed deep events when its task bridge is configured.

Paperclip can also run the complete pipeline through its HTTP adapter. Point the
adapter to `http://127.0.0.1:8787/api/paperclip/analyze` and give it the header
`Authorization: Bearer <your PAPERCLIP_BRIDGE_TOKEN>`. The endpoint accepts the
standard Paperclip run envelope and reads optional `symbol`, `market`, `venue`, and
`equity` values from `context`; otherwise it uses the safe defaults in `.env`.

If an upstream is missing, incompatible, or temporarily unavailable, analysis degrades
to a visible, non-executable HOLD result instead of crashing the Analyse page. Crypto
quotes try WEEX first, then optional Yahoo Finance and OpenBB research feeds; research
providers receive normalized symbols such as `BTC-USD`, while exchange and risk records
continue to use `BTCUSDT`.

| Layer | Upstream | Responsibility |
|---|---|---|
| Data | OpenBB | Point-in-time prices, fundamentals, macro and news inputs |
| Intelligence | TradingAgents | Multi-agent research and a BUY/SELL/HOLD proposal |
| Control plane | Paperclip | Schedules, agent work, budgets, approvals and audit visibility |
| Safety boundary | This project | Deterministic risk checks and broker execution |

## Market and venue selection

Current registry choices are:

| Market | Venue | Status |
|---|---|---|
| Crypto spot | WEEX V3 | Public live data; execution intentionally paper-only |
| Crypto futures | WEEX V3 | Mark-price data, symbols, signed demo orders and reconciliation |
| Equities | OpenBB | Data adapter available |
| Forex | OpenBB | Registry/data route available |
| Commodities | OpenBB | Registry/data route available |
| Options | OpenBB | Registry/data route available |
| Indian indices | Yahoo Finance → OpenBB | BANK NIFTY, NIFTY 50 and related Indian bank stocks |

Spot and futures remain separate adapters because WEEX exposes them through different
domains and schemas. Additional venues can register implementations without modifying
the strategy or risk engine.

The AI only creates a `TradeSignal`. A separate deterministic risk engine creates an
`OrderIntent`, and only the broker adapter can execute it. Version 0.2 ships with a
stateful in-memory paper broker and no live-order adapter.

The WEEX demo broker signs V3 requests, uses only `/capi/v3/sim/*`, requires an attached
stop loss, rejects duplicate client order IDs, reconciles positions before every order,
caps configured leverage at 5x, and permits isolated margin only. Exit-only sizing is
enforced locally because the documented demo order schema does not expose `reduceOnly`.
That race-sensitive limitation is another reason live execution remains unavailable.

## Run locally

Python 3.11 or 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,upstreams]'
cp .env.example .env
pytest
tradebot BTCUSDT --market crypto_futures --venue weex --date 2026-09-01 --equity 100000
```

OpenBB must be reachable at `OPENBB_API_URL` (default `http://127.0.0.1:6900`).
TradingAgents package layouts are detected at runtime and failures (including its LLM
provider) become safe HOLD results. TradingAgents requires an enabled LLM provider key
for full analysis. Paperclip is an optional control and audit layer; Signal and Paper
Trading continue when it is disabled or unreachable. `Optional · Off` means it was
intentionally disabled, `Connected` means the configured bridge is ready, and
`Temporarily unavailable` means an enabled bridge could not be reached. Configure
`PAPERCLIP_ENABLED`, `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, and
`PAPERCLIP_COMPANY_ID`; the legacy `PAPERCLIP_TASK_BRIDGE_URL` remains the pinned
v0.3.1 event target. Never commit its key.

Set `OPENAI_MODEL` to the model available to your OpenAI project (the default is
`gpt-4o-mini`). The `/debug` endpoint validates that model against OpenAI when a key is
present and reports OpenAI, TradingAgents, OpenBB, WEEX, Yahoo and Paperclip health,
including the last success and exact last error. TradingAgents also logs each pipeline
stage without logging secret values. Paperclip remains hidden in the home-page health
strip until its inbound or outbound bridge is explicitly enabled.

The BANKNIFTY options view uses one normalized, read-only provider contract. Provider
priority is authenticated DhanHQ, configured OpenBB, public NSE, then an explicit
temporary-unavailable response. Configure `BANKNIFTY_OPTIONS_PROVIDER=dhan`, blank
`DHAN_CLIENT_ID`/`DHAN_ACCESS_TOKEN` placeholders, and optionally
`DHAN_API_BASE_URL`; missing Dhan credentials fall through automatically. Dhan is
market-data-only: no order, portfolio, or execution endpoint is present. A future
provider can implement `OptionChainProvider` without changing the route or UI.

The options asset view exposes expiration and strike
selection, open interest, implied volatility, available Greeks, put/call ratio and max
pain. Fields without provider data are rendered as unavailable cells, while selectors
and summary widgets with no data are omitted.

WEEX spot and futures are kept as separate complete lightweight universes. Search first
filters ranked/loaded rows, then queries the cached genuine WEEX universe, including
active low-volume instruments outside the bounded initial ranking view. “Available on
WEEX” does not mean “ranked opportunity”; an unranked result shows `Not scored yet` and
can run Quick Signal on demand. Use the refresh controls to rebuild caches. Local setup
can be verified with `pytest -q`, `python -m compileall -q src tests`, and a temporary
`SIGNAL_DB_PATH` before starting `signal-app`.

Run the two upstream smoke tests independently before debugging the full application:

```bash
python scripts/test_openai.py
python scripts/test_tradingagents.py
```

Both use the same `.env` and `OPENAI_MODEL` as the server, print their raw response,
and exit nonzero with a complete Python traceback if an import, credential, model, or
upstream request fails.

## Safety gates before any live broker

1. Immutable event/audit storage and idempotency keys.
2. Point-in-time backtests with fees, spread and slippage.
3. Walk-forward and out-of-sample evaluation against a simple benchmark.
4. Persistent positions, reconciliation, market-hours checks and stale-data rejection.
5. Kill switch, daily-loss circuit breaker, exposure/concentration limits and manual approval.
6. Broker sandbox soak test, then tiny-notional canary. Never hand an LLM broker credentials.

For WEEX specifically, use a separate IP-allowlisted API key with only the required
trade permission, no withdrawal capability, and begin with the futures simulation API.

## Licensing

Pinned revisions are listed in `UPSTREAMS.lock`. TradingAgents is Apache-2.0 and
Paperclip is MIT. OpenBB is AGPL-3.0-only; this design consumes OpenBB across a
service/API boundary. Deployment and distribution obligations should be reviewed by
qualified counsel. This software is experimental and is not investment advice.

## Automated WEEX research and background paper monitoring

The **Scanner** navigation page prefilters genuine active WEEX spot and futures
universes separately using reported turnover (liquidity proxy) and absolute
24-hour movement plus the reported high/low range (movement is the volatility
proxy when the range is unavailable). It analyses up to 12 candidates per market
through the existing multi-timeframe Quick Signal engine, with two concurrent
symbols and at least 250 ms between symbol starts. Provider requests retain their
transport timeouts, and universe/candle/Quick Signal caches protect repeated reads.
Manual runs return HTTP 409 while another scan is running. Scheduled scans default
to five minutes; an interval is measured after completion, so scans cannot overlap.
No scanner result automatically opens a position.

Results and validated risk settings live in the existing local SQLite database
(`SIGNAL_DB_PATH`, default `data/signal.db`), never in Git. A restart retains the
last useful results; unavailable markets retain their old rows with stale flags
and original data timestamps. Initial outages show unavailable/empty states,
without invented instruments or prices. Funding is reported only when the WEEX
bulk ticker supplies it. Order-book imbalance and open interest/change are
currently explicitly unavailable; scanner confidence is reduced for missing
supplemental fields. These are heuristic research confidence/probability values,
not calibrated return guarantees.

The application lifespan starts a separate paper monitor (default 60 seconds).
It refreshes genuine quotes and reuses saved stop/target trigger logic. SQLite
transactions prevent duplicate closes across manual and background operations.
Automatic fills preserve the existing simulated stop/target prices; closed trades
also record the actual evaluated quote, evaluation time and provider. Quote outages
retain stored marks, visibly flagged stale. Paperclip is optional and fail-open.
Stop workers through normal application shutdown; use **one Uvicorn worker** so
there is one scanner/monitor scheduler for a database. No real orders, withdrawals,
broker execution or credential routes are added.

Paper Trading exposes locally saved risk settings. Percentage settings use
0–100 units. Existing 1x margin/account calculations are preserved by default;
allocation/exposure/loss ceilings default to 100%, position count to 100, stop/target
fallbacks to 2%/4%. Signal-provided levels take precedence. The optional 1–10x
**simulated paper leverage** multiplies futures quantity/P&L only; spot remains
1x. Margin reserves the entered amount, while exposure checks use full leveraged
notional. Leverage is saved per position, so later setting changes do not alter
existing trades. The loss circuit breaker measures net UTC-day realized P&L against
the original starting balance. These controls never enable live execution.

### Research API

All responses are JSON; HTTP 422 indicates invalid settings, HTTP 409 a duplicate
manual scan, and HTTP 202 an accepted background scan. Provider errors are sanitized.

| Endpoint | Contract |
| --- | --- |
| `GET /api/scanner/status` | running/state, last completed, duration, considered/analysed counts, next scan, cumulative failures, stale |
| `GET /api/scanner/results` | results, per-market availability, data timestamp, attempt time, stale |
| `POST /api/scanner/run` | accepts a bounded run; concurrent submissions return 409 |
| `GET /api/paper/monitor/status` | running/state, last successful evaluation, next evaluation, stale, failures |
| `GET /api/paper/settings` | complete locally persisted risk settings |
| `PUT /api/paper/settings` | partial object of validated known settings; returns complete settings |
| `GET /api/status` | paper mode, existing integrations, scanner and monitor diagnostics |

Result rows include market/symbol/action, opportunity score, confidence/probability,
risk, entry reference, stop/target, suggested allocation fraction, timeframe rows,
momentum/volatility summaries, supplemental nullable values, explanation and timestamp.
Missing supplemental values are `null` and named in `unavailable_fields`, never fake zeroes.
Interval ranges are scanner 60–86400 seconds and monitor 5–3600 seconds; position
count 1–1000; allocation/exposure/loss percentages 0.1–100; stop 0.1–50%; target
0.1–100%; futures simulated leverage 1–10x. Interval changes apply after the current
wait. Existing positions retain their saved levels.

### Local verification and CI protection

```bash
python -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src tests
node --check src/tradebot/web/app.js
git ls-files -z '*.js' | xargs -0 -r -n 1 node --check
git diff --check
python -m uvicorn tradebot.app:app --host 127.0.0.1 --port 8787
```

The mocked tests cover HTML/API smoke routes, JavaScript parsing and a separate
15-second shell loading-recovery message. The service-worker shell cache is versioned,
uses network-first revalidation, activates promptly and removes only older Signal
shell caches. CI runs on every pull request and push to main with no live provider
credentials required. Configure the GitHub `main` branch ruleset to require
**Required paper regression checks** before merging, with branch deletion/force pushes
blocked as appropriate. A workflow file alone cannot enforce mandatory branch
protection; that requires repository administration access.

Public research requires HTTPS access to `api-spot.weex.com` and
`api-contract.weex.com`. Provider outages remain explicit, and BANKNIFTY keeps its
existing research-only provider behavior.
