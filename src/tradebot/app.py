import importlib.util
import os
import secrets
import json
import webbrowser
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters import OpenBBClient, PaperclipReporter, TradingAgentsClient
from .analysis import (CandleCache, DeepJobRegistry, FastAIExplainer, QuickResultCache,
                       QuickSignalEngine, TIMEFRAMES, deterministic_fast_explanation,
                       normalize_deep_reasoning)
from .assets import asset_metadata, public_metadata
from .banknifty_options import NSEOptionChainClient, UNAVAILABLE_MESSAGE, build_chain
from .diagnostics import diagnostics
from .config import load_project_env
from .execution import PaperBroker
from .models import MarketKind, MarketSelection, MarketSnapshot
from .overview import market_overview
from .paper import PaperStore
from .risk import RiskEngine, RiskLimits
from .service import TradingService
from .venues import default_registry


WEB_DIR = Path(__file__).with_name("web")


load_project_env()
signals = TradingAgentsClient()
quick_signals = QuickSignalEngine()
deep_jobs = DeepJobRegistry()
candle_cache = CandleCache()
quick_results = QuickResultCache()
fast_ai = FastAIExplainer()


def _debug_enabled() -> bool:
    return os.getenv("SIGNAL_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def _provider_failure_category(*errors: Exception) -> str:
    text = " ".join(str(error).lower() for error in errors)
    if any(word in text for word in ("blocked", "forbidden", "403", "unauthorized", "401")):
        return "provider_unavailable"
    if any(word in text for word in ("empty", "no usable", "no banknifty", "no option")):
        return "provider_returned_empty"
    if any(word in text for word in ("market closed", "stale")):
        return "market_closed_or_no_chain"
    if errors and all(any(word in str(error).lower() for word in
                          ("not configured", "missing configuration", "no provider"))
                      for error in errors):
        return "not_configured"
    return "provider_unavailable" if errors else "unknown_failure"


def _banknifty_explanation(category: str) -> str:
    return {
        "not_configured": "Bank Nifty options require a configured option-chain provider.",
        "provider_unavailable": "Provider attempts completed, but a valid option chain could not be retrieved.",
        "provider_returned_empty": "Provider attempts completed, but no valid option-chain rows were returned.",
        "market_closed_or_no_chain": "No current option chain is available; the market may be closed or between listed chains.",
        "unknown_failure": "The option chain could not be verified right now. Please retry shortly.",
    }[category]


def _provider_error(public_message: str, exc: Exception) -> HTTPException:
    """Return a stable user error without disclosing provider internals."""
    diagnostics.failure("provider", exc)
    detail = public_message
    if _debug_enabled():
        detail += f" ({type(exc).__name__}: {exc})"
    return HTTPException(502, detail)


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._:/-]+$")
    market: MarketKind = MarketKind.CRYPTO_FUTURES
    venue: str = Field(default="weex", min_length=1, max_length=32)
    as_of: str = Field(default_factory=lambda: date.today().isoformat())
    equity: float = Field(default=100_000, gt=0, le=100_000_000)


class DeepAnalyzeRequest(AnalyzeRequest):
    refresh: bool = False


class PaperclipRunRequest(BaseModel):
    runId: str | None = None
    agentId: str | None = None
    companyId: str | None = None
    context: dict = Field(default_factory=dict)


class OpenPaperPositionRequest(BaseModel):
    market: MarketKind
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._:/-]+$")
    side: str | None = Field(default=None, pattern=r"^(LONG|SHORT)$")
    notional_amount: float | None = Field(default=None, gt=0, le=1_000_000_000)
    force: bool = False


class ClosePaperPositionRequest(BaseModel):
    close_reason: str = Field(default="Closed by user", max_length=200)


class WatchlistRequest(BaseModel):
    market: MarketKind
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._:/-]+$")
    display_name: str | None = Field(default=None, max_length=100)


class JournalRequest(BaseModel):
    note: str = Field(min_length=1, max_length=4000)
    position_id: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=32)


def integration_status() -> dict:
    paperclip = PaperclipReporter()
    openbb_configured = bool(os.getenv("OPENBB_API_URL"))
    tradingagents_installed = importlib.util.find_spec("tradingagents") is not None
    tradingagents_configured = any(os.getenv(key) for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"))
    # PAPERCLIP_API_URL identifies the upstream service but does not provide an
    # authenticated path in either direction.  Signal is ready to integrate only
    # when Paperclip can call its protected endpoint, or when Signal can report to
    # an authenticated task bridge.
    paperclip_configured = bool(os.getenv("PAPERCLIP_BRIDGE_TOKEN") or paperclip.configured)
    return {
        "openbb": {
            # OpenBB is consumed as a service, so its Python package need not be local.
            "installed": True,
            "configured": openbb_configured,
            "ready": openbb_configured,
            "role": "market data and research layer",
        },
        "tradingagents": {
            "installed": tradingagents_installed,
            "configured": tradingagents_configured,
            "ready": tradingagents_installed and tradingagents_configured,
            "role": "optional advanced research layer",
        },
        "paperclip": {
            "installed": True,
            "configured": paperclip_configured,
            "ready": paperclip_configured,
            "enabled": paperclip.enabled or bool(os.getenv("PAPERCLIP_BRIDGE_TOKEN")),
            "role": "optional control and audit bridge",
        },
        "weex": {
            "installed": True,
            "configured": True,
            "ready": True,
            "demo_credentials": all(os.getenv(key) for key in (
                "WEEX_API_KEY", "WEEX_SECRET_KEY", "WEEX_PASSPHRASE")),
            "role": "crypto venue and data source",
        },
    }


def verify_openai_model() -> None:
    """Ask OpenAI whether the configured model is available to this API key."""
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return
    model = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"
    request = Request(f"https://api.openai.com/v1/models/{quote(model, safe='')}",
                      headers={"Authorization": f"Bearer {key}"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if payload.get("id") != model:
            raise RuntimeError(f"OpenAI returned an unexpected model id for {model}")
        diagnostics.success("openai")
    except Exception as exc:
        diagnostics.failure("openai", exc)


def startup_diagnostics() -> None:
    """Print a safe, secret-free integration inventory on every server start."""
    from importlib import metadata
    try:
        graph_class = __import__("tradebot.adapters", fromlist=["_import_attribute"])._import_attribute((
            ("tradingagents.graph.trading_graph", "TradingAgentsGraph"),
            ("tradingagents.graph", "TradingAgentsGraph"),
            ("tradingagents", "TradingAgentsGraph"),
        ))
        imported, module_path = True, f"{graph_class.__module__}.{graph_class.__name__}"
    except Exception as exc:
        imported, module_path = False, f"unavailable ({type(exc).__name__}: {exc})"
    try:
        version = metadata.version("tradingagents")
    except metadata.PackageNotFoundError:
        version = "not-installed"
    paperclip = PaperclipReporter()
    print(f"OpenAI model: {os.getenv('OPENAI_MODEL', '') or 'gpt-4o-mini'}", flush=True)
    print(f"OpenAI key loaded: {bool(os.getenv('OPENAI_API_KEY'))}", flush=True)
    print(f"TradingAgents imported: {imported}", flush=True)
    print(f"TradingAgents version: {version}", flush=True)
    print(f"Configured LLM: {os.getenv('OPENAI_MODEL', '') or 'gpt-4o-mini'}", flush=True)
    print(f"module path: {module_path}", flush=True)
    print(f"package version: {version}", flush=True)
    print(f"loaded classes: {module_path if imported else 'none'}", flush=True)
    print(f"Paperclip enabled: {paperclip.enabled}", flush=True)
    print("WEEX enabled: True", flush=True)
    print(f"OpenBB enabled: {bool(os.getenv('OPENBB_API_URL'))}", flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup_diagnostics()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Signal", version="0.4.0", lifespan=lifespan)
    paper = PaperStore()
    app.state.paper_store = paper
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(WEB_DIR / "manifest.webmanifest")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")

    @app.get("/api/status")
    def status():
        return {"mode": "paper", "integrations": integration_status()}

    @app.get("/debug")
    def debug():
        verify_openai_model()
        states = integration_status()
        result = diagnostics.snapshot(("openai", "tradingagents", "openbb", "weex", "yahoo", "paperclip"))
        for name, item in result.items():
            item["configured"] = states.get(name, {}).get("configured", name in {"weex", "yahoo"})
            if not item["configured"] and item["status"] == "not_checked":
                item["status"] = "disabled" if name == "paperclip" else "not_configured"
        result["openai"].update(model=os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini",
                                api_key_loaded=bool(os.getenv("OPENAI_API_KEY")))
        if not _debug_enabled():
            for item in result.values():
                item.pop("last_error", None)
                item.pop("last_traceback", None)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/options/{symbol}")
    def options(symbol: str, expiration: str | None = None):
        try:
            return OpenBBClient().option_chain(symbol, expiration)
        except Exception as exc:
            diagnostics.failure("openbb", exc)
            raise _provider_error("Option chain is temporarily unavailable. Try again later.", exc) from exc

    @app.get("/api/banknifty-options")
    def banknifty_options(expiry: str | None = None, option_type: str | None = None,
                          moneyness: str | None = None):
        """Return a genuine OpenBB/NSE chain or an explicit unavailable state."""
        try:
            provider = OpenBBClient(asset_class="index")
            raw = provider.option_chain("BANKNIFTY", expiry)
            if not raw.get("contracts"):
                raise ValueError("OpenBB returned no BANKNIFTY option contracts")
            spot = provider.snapshot("^NSEBANK").price
        except Exception as exc:
            diagnostics.failure("openbb", exc)
            try:
                raw = NSEOptionChainClient().option_chain(expiry)
                if not raw.get("contracts") or raw.get("underlying_price") is None:
                    raise ValueError("NSE returned no usable BANKNIFTY option contracts")
                spot = raw["underlying_price"]
            except Exception as nse_exc:
                diagnostics.failure("nse", nse_exc)
                category = _provider_failure_category(exc, nse_exc)
                result = {"available": False, "message": UNAVAILABLE_MESSAGE, "symbol": "BANKNIFTY",
                        "underlying_symbol": "^NSEBANK", "contracts": [], "expiries": [],
                        "research_only": True, "provider_attempts": {"openbb": True, "nse_fallback": True},
                        "failure_category": category, "explanation": _banknifty_explanation(category),
                        "provider_status": ("not_configured" if category == "not_configured"
                                            else "temporarily_unavailable"),
                        "last_checked": datetime.now(timezone.utc).isoformat(),
                        "setup_note": "A reliable options data provider is required for production Bank Nifty option-chain coverage."}
                if _debug_enabled():
                    result["provider_errors"] = {"openbb": f"{type(exc).__name__}: {exc}",
                                                 "nse_fallback": f"{type(nse_exc).__name__}: {nse_exc}"}
                return result
        result = build_chain(raw, spot, expiry, option_type, moneyness)
        # Filters may legitimately select no contracts while the provider remains available.
        result["available"] = True
        result["provider_status"] = "connected"
        return result

    @app.get("/api/markets")
    def markets():
        return default_registry().choices()

    @app.get("/api/assets/{market}/{symbol}/metadata")
    def metadata(market: MarketKind, symbol: str):
        return public_metadata(market, symbol)

    @app.get("/api/overview/{market}")
    def overview(market: MarketKind, refresh: bool = False):
        try:
            return market_overview(market, refresh=refresh)
        except Exception as exc:
            raise _provider_error("Market data is temporarily unavailable. Try again later.", exc) from exc

    def market_data(request: AnalyzeRequest):
        selection = MarketSelection(request.market, request.venue, request.symbol.upper())
        return default_registry().market_data(selection)

    def run_deep_analysis(request: AnalyzeRequest, quick: dict | None = None):
        try:
            provider = market_data(request)
            if ((request.market is MarketKind.INDIAN_INDICES or request.symbol.upper().endswith(".NS"))
                    and not (quick or {}).get("chart_timeframes")):
                try:
                    bars = provider.candles(request.symbol.upper(), "1d", 60)
                    if len(bars) < 20:
                        raise ValueError("incomplete OHLCV history")
                except Exception:
                    return {"ai_available": False, "ai_notice":
                            "Deep AI unavailable for this Indian asset because provider OHLCV data is incomplete. Quick Signal remains available.",
                            "research_only": True}
            service = TradingService(
                provider,
                signals,
                RiskEngine(RiskLimits()),
                PaperBroker(request.equity),
                PaperclipReporter(),
            )
            market = None
            if quick and quick.get("market"):
                market = MarketSnapshot(**quick["market"])
            result = service.run(request.symbol, request.as_of, request.equity, market=market)
            result["integrations"] = integration_status()
            result["notice"] = "Research and paper-risk decision only. No live order was placed."
            result.update(public_metadata(request.market, request.symbol))
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise _provider_error("Deep AI could not complete. Quick Signal remains available.", exc) from exc

    @app.post("/api/analyze")
    def analyze(request: AnalyzeRequest):
        """Backward-compatible fast endpoint; deep research is explicitly opt-in."""
        return quick_analyze(request)

    @app.post("/api/analyze/quick")
    def quick_analyze(request: AnalyzeRequest):
        try:
            provider = market_data(request)
            symbol = request.symbol.upper()
            snapshot = provider.snapshot(symbol)
            histories, warnings = {}, []
            # Timeframes load concurrently under a tight provider timeout. A partial
            # response is useful and a total candle failure retains the old fast path.
            timeframes = (("5m", "15m", "1h", "1d")
                          if request.market is MarketKind.INDIAN_INDICES else TIMEFRAMES)
            executor = ThreadPoolExecutor(max_workers=len(timeframes), thread_name_prefix="quick-candles")
            futures = {executor.submit(candle_cache.get_or_load, f"{request.market.value}:{symbol}", frame,
                       lambda f=frame: provider.candles(symbol, f, 250)): frame
                       for frame in timeframes}
            candle_timeout = max(.05, float(os.getenv("SIGNAL_QUICK_CANDLE_TIMEOUT_SECONDS", "8")))
            try:
                done, pending = wait(futures, timeout=candle_timeout)
                for future in done:
                    frame = futures[future]
                    try:
                        histories[frame] = future.result()
                    except Exception as exc:
                        warnings.append(f"{frame} candles unavailable: {type(exc).__name__}")
                for future in pending:
                    future.cancel()
                    warnings.append(f"{futures[future]} candles unavailable: timeout")
            finally:
                # A slow upstream must not hold the request open. Running urllib calls
                # finish under their adapter timeout and cannot safely be killed.
                executor.shutdown(wait=False, cancel_futures=True)
            result = quick_signals.analyze(snapshot, request.equity, histories)
            result.update(public_metadata(request.market, symbol))
            result.update({"live_price": snapshot.price, "change_24h": snapshot.change_24h,
                           "volume": snapshot.volume, "source": snapshot.source,
                           "last_updated": snapshot.as_of})
            # Send the candles already fetched for signal generation to the client.
            # This makes timeframe changes instant and avoids a second provider call.
            result["chart_timeframes"] = {
                frame: [
                    {"timestamp": bar.timestamp, "open": bar.open, "high": bar.high,
                     "low": bar.low, "close": bar.close, "volume": bar.volume}
                    for bar in bars[-80:]
                ]
                for frame, bars in histories.items() if bars
            }
            default_frame = next((frame for frame in ("1h", "15m", "5m", "4h", "1d", "1m")
                                  if result["chart_timeframes"].get(frame)), None)
            result["chart_default_timeframe"] = default_frame
            # Keep the original close-only field for older clients.
            result["chart_points"] = ([{"timestamp": bar["timestamp"], "close": bar["close"]}
                                       for bar in result["chart_timeframes"][default_frame]]
                                      if default_frame else [])
            if warnings:
                result["warnings"] = warnings
            result["notice"] = "Deterministic quick signal only. No AI or live order was used."
            quick_results.put(request.market.value, symbol, result)
            return result
        except Exception as exc:
            if os.getenv("SIGNAL_DEBUG", "").lower() in {"1", "true", "yes", "on"}:
                detail = f"Live quick signal could not load: {type(exc).__name__}: {exc}"
            elif request.market is MarketKind.COMMODITIES:
                name = asset_metadata(request.market, request.symbol).display_name.removesuffix(" futures")
                detail = f"Live data for {name} is temporarily unavailable. Try again later or choose another commodity."
            elif request.market is MarketKind.FOREX:
                detail = "Forex data is temporarily unavailable. Try again later."
            elif request.market in (MarketKind.EQUITIES, MarketKind.INDIAN_INDICES):
                detail = "Equity data is temporarily unavailable. Try again later."
            else:
                detail = "Live market data is temporarily unavailable. Try again later."
            raise HTTPException(502, detail) from exc

    @app.post("/api/analyze/summary")
    def summary_analyze(request: AnalyzeRequest):
        """Quick Signal's optional fast explanation, with a deterministic timeout fallback."""
        quick = (quick_results.get(request.market.value, request.symbol)
                 or quick_analyze(request))
        explanation, watch = deterministic_fast_explanation(quick)
        fallback_used = False
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fast-ai")
        future = executor.submit(fast_ai.explain, quick)
        try:
            timeout = max(.01, float(os.getenv("SIGNAL_FAST_AI_TIMEOUT_SECONDS", "15")))
            ai_explanation = future.result(timeout=timeout)
        except Exception:
            ai_explanation, fallback_used = explanation, True
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        signal, plan = quick["signal"], quick["risk_plan"]
        return {"main_signal_action": str(getattr(signal.get("side"), "value", signal.get("side"))),
                "confidence": signal.get("confidence"), "risk_score": plan.get("risk_score"),
                "live_price": quick.get("live_price"), "risk_plan": plan,
                "ai_explanation": ai_explanation, "what_to_watch_next": watch,
                "fallback_used": fallback_used, "ai_mode": "fast_explanation"}

    @app.post("/api/analyze/deep")
    def deep_analyze(request: DeepAnalyzeRequest):
        quick_context = {}

        def perform(progress):
            progress(1, "Preparing market data")
            quick = (quick_results.get(request.market.value, request.symbol)
                     or quick_analyze(request))
            quick_context["result"] = quick
            progress(2, "Checking technical indicators")
            progress(3, "Running TradingAgents research")
            deep = run_deep_analysis(request, quick)
            if deep.get("ai_available") is False:
                raise RuntimeError(deep.get("ai_notice") or "Deep AI is unavailable")
            progress(4, "Building plain-language summary")
            merged = {**quick, **deep, "quick_signal": quick}
            for key in ("live_price", "change_24h", "volume", "source", "last_updated",
                        "timeframe_breakdown", "key_levels", "trend_summary",
                        "momentum_summary", "volatility_summary", "chart_points",
                        "chart_timeframes", "chart_default_timeframe"):
                merged[key] = quick.get(key)
            progress(5, "Finalizing decision")
            result = normalize_deep_reasoning(merged, quick)
            result.update({"mode": "deep", "cached": False,
                           "deep_analyzed_at": datetime.now(timezone.utc).isoformat()})
            return result

        def fallback():
            quick = (quick_context.get("result") or
                     quick_results.get(request.market.value, request.symbol) or quick_analyze(request))
            explanation, watch = deterministic_fast_explanation(quick)
            return {**quick, "fast_ai_explanation": {"main_signal_action": quick["signal"]["side"],
                    "ai_explanation": explanation, "what_to_watch_next": watch,
                    "fallback_used": True, "ai_mode": "fast_explanation"}}

        return deep_jobs.start(request.market.value, request.symbol, perform, fallback,
                               refresh=request.refresh)

    def _paper_quote(market: str, symbol: str) -> float:
        selection = MarketSelection(MarketKind(market), "weex" if market.startswith("crypto_") else "openbb",
                                    symbol.upper())
        price = float(default_registry().market_data(selection).snapshot(symbol.upper()).price)
        if price <= 0:
            raise ValueError("Live price is missing or invalid")
        return price

    def _mark_open_positions() -> list[dict]:
        for position in paper.positions():
            try:
                paper.mark(position["id"], _paper_quote(position["market"], position["symbol"]))
            except Exception:
                # Preserve the last valid mark when a provider is temporarily unavailable.
                pass
        return paper.positions()

    @app.get("/api/paper/account")
    def paper_account():
        _mark_open_positions()
        return paper.account()

    @app.get("/api/paper/positions")
    def paper_positions():
        return _mark_open_positions()

    @app.post("/api/paper/positions", status_code=201)
    def open_paper_position(request: OpenPaperPositionRequest):
        analyze_request = AnalyzeRequest(symbol=request.symbol, market=request.market,
                                         venue="weex" if request.market.value.startswith("crypto_") else "openbb")
        quick = quick_results.get(request.market.value, request.symbol) or quick_analyze(analyze_request)
        raw_action = quick["signal"].get("side", "HOLD")
        action = str(getattr(raw_action, "value", raw_action)).upper()
        if action == "HOLD" and not request.force:
            raise HTTPException(409, "HOLD has no active trade setup. Add it to your watchlist or explicitly use force=true.")
        side = request.side or ("LONG" if "BUY" in action else "SHORT" if "SELL" in action else None)
        if side is None:
            raise HTTPException(422, "Choose LONG or SHORT when forcing a HOLD signal")
        price = float(quick.get("live_price") or 0)
        if price <= 0:
            raise HTTPException(422, "A valid live price is required for a paper trade")
        plan = quick.get("risk_plan") or {}
        default_notional = paper.account()["equity"] * float(plan.get("position_size_pct") or .01)
        notional = request.notional_amount if request.notional_amount is not None else default_notional
        try:
            return paper.open_position(market=request.market.value, symbol=request.symbol,
                    display_name=quick.get("display_name") or request.symbol.upper(), side=side,
                    price=price, notional=notional, signal=quick["signal"], risk_plan=plan)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/paper/positions/{position_id}/close")
    def close_paper_position(position_id: str, request: ClosePaperPositionRequest):
        position = next((x for x in paper.positions() if x["id"] == position_id), None)
        if not position:
            raise HTTPException(404, "Open paper position not found")
        try:
            return paper.close_position(position_id, _paper_quote(position["market"], position["symbol"]),
                                        request.close_reason)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/paper/trades")
    def paper_trades():
        return paper.trades()

    @app.get("/api/paper/watchlist")
    def paper_watchlist():
        return paper.watchlist()

    @app.post("/api/paper/watchlist", status_code=201)
    def add_paper_watchlist(request: WatchlistRequest):
        item = request.model_dump(mode="json")
        item["market"] = request.market.value
        cached = quick_results.get(request.market.value, request.symbol)
        if cached:
            latest_action = cached["signal"].get("side")
            item.update(latest_action=str(getattr(latest_action, "value", latest_action)),
                        latest_confidence=cached["signal"].get("confidence"), latest_price=cached.get("live_price"))
            item["display_name"] = item.get("display_name") or cached.get("display_name")
        return paper.add_watchlist(item)

    @app.delete("/api/paper/watchlist/{market}/{symbol}", status_code=204)
    def delete_paper_watchlist(market: MarketKind, symbol: str):
        if not paper.delete_watchlist(market.value, symbol):
            raise HTTPException(404, "Watchlist item not found")

    @app.get("/api/paper/journal")
    def paper_journal():
        return paper.journal()

    @app.post("/api/paper/journal", status_code=201)
    def add_paper_journal(request: JournalRequest):
        try:
            return paper.add_note(request.note, request.position_id, request.symbol)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/analyze/deep/status/{job_id}")
    def deep_status(job_id: str):
        job = deep_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Deep AI job not found")
        return job

    @app.post("/api/paperclip/analyze")
    def paperclip_analyze(payload: PaperclipRunRequest, authorization: str = Header(default="")):
        expected = os.getenv("PAPERCLIP_BRIDGE_TOKEN", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "Paperclip bridge authorization failed")
        context = payload.context
        request = AnalyzeRequest(
            symbol=str(context.get("symbol") or os.getenv("DEFAULT_SYMBOL", "BTCUSDT")),
            market=MarketKind(str(context.get("market") or os.getenv("DEFAULT_MARKET", "crypto_futures"))),
            venue=str(context.get("venue") or os.getenv("DEFAULT_VENUE", "weex")),
            equity=float(context.get("equity") or os.getenv("PAPER_STARTING_CASH", "100000")),
        )
        return {"paperclip_run_id": payload.runId, "result": run_deep_analysis(request)}

    @app.get("/{path:path}")
    def app_route(path: str):
        if path.startswith("api/") or path == "debug":
            raise HTTPException(404, "API route not found")
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    host = "127.0.0.1"
    port = int(os.getenv("SIGNAL_PORT", "8787"))
    webbrowser.open(f"http://{host}:{port}")
    uvicorn.run("tradebot.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
