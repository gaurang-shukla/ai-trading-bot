# Signal — three-layer AI trading app

An initial, functional **paper-trading** vertical slice built around the three requested projects.
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
for full analysis. Paperclip reporting is disabled
unless `PAPERCLIP_TASK_BRIDGE_URL` and its scoped key are explicitly configured.

Set `OPENAI_MODEL` to the model available to your OpenAI project (the default is
`gpt-4o-mini`). The `/debug` endpoint validates that model against OpenAI when a key is
present and reports OpenAI, TradingAgents, OpenBB, WEEX, Yahoo and Paperclip health,
including the last success and exact last error. TradingAgents also logs each pipeline
stage without logging secret values. Paperclip remains hidden in the home-page health
strip until its inbound or outbound bridge is explicitly enabled.

The options asset view obtains its chain from OpenBB and exposes expiration and strike
selection, open interest, implied volatility, available Greeks, put/call ratio and max
pain. Fields without provider data are rendered as unavailable cells, while selectors
and summary widgets with no data are omitted.

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
