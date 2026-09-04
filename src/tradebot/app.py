import importlib.util
import os
import secrets
import json
import webbrowser
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters import OpenBBClient, PaperclipReporter, TradingAgentsClient
from .analysis import DeepAnalysisCache, QuickSignalEngine
from .diagnostics import diagnostics
from .config import load_project_env
from .execution import PaperBroker
from .models import MarketKind, MarketSelection
from .overview import market_overview
from .risk import RiskEngine, RiskLimits
from .service import TradingService
from .venues import default_registry


WEB_DIR = Path(__file__).with_name("web")


load_project_env()
signals = TradingAgentsClient()
quick_signals = QuickSignalEngine()
deep_cache = DeepAnalysisCache()


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
            "role": "normalized market research",
        },
        "tradingagents": {
            "installed": tradingagents_installed,
            "configured": tradingagents_configured,
            "ready": tradingagents_installed and tradingagents_configured,
            "role": "multi-agent market decision",
        },
        "paperclip": {
            "installed": True,
            "configured": paperclip_configured,
            "ready": paperclip_configured,
            "enabled": paperclip.enabled or bool(os.getenv("PAPERCLIP_BRIDGE_TOKEN")),
            "role": "audit and orchestration bridge",
        },
        "weex": {
            "installed": True,
            "configured": True,
            "ready": True,
            "demo_credentials": all(os.getenv(key) for key in (
                "WEEX_API_KEY", "WEEX_SECRET_KEY", "WEEX_PASSPHRASE")),
            "role": "crypto prices and demo execution",
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
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/options/{symbol}")
    def options(symbol: str, expiration: str | None = None):
        try:
            return OpenBBClient().option_chain(symbol, expiration)
        except Exception as exc:
            diagnostics.failure("openbb", exc)
            raise HTTPException(502, f"Option chain could not load: {type(exc).__name__}: {exc}") from exc

    @app.get("/api/markets")
    def markets():
        return default_registry().choices()

    @app.get("/api/overview/{market}")
    def overview(market: MarketKind):
        try:
            return market_overview(market)
        except Exception as exc:
            raise HTTPException(502, f"Market overview could not load: {exc}") from exc

    def market_data(request: AnalyzeRequest):
        selection = MarketSelection(request.market, request.venue, request.symbol.upper())
        return default_registry().market_data(selection)

    def run_deep_analysis(request: AnalyzeRequest):
        try:
            service = TradingService(
                market_data(request),
                signals,
                RiskEngine(RiskLimits()),
                PaperBroker(request.equity),
                PaperclipReporter(),
            )
            result = service.run(request.symbol, request.as_of, request.equity)
            result["integrations"] = integration_status()
            result["notice"] = "Research and paper-risk decision only. No live order was placed."
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Analysis could not complete: {exc}") from exc

    @app.post("/api/analyze")
    def analyze(request: AnalyzeRequest):
        """Backward-compatible fast endpoint; deep research is explicitly opt-in."""
        return quick_analyze(request)

    @app.post("/api/analyze/quick")
    def quick_analyze(request: AnalyzeRequest):
        try:
            snapshot = market_data(request).snapshot(request.symbol.upper())
            result = quick_signals.analyze(snapshot, request.equity)
            result["notice"] = "Deterministic quick signal only. No AI or live order was used."
            return result
        except Exception as exc:
            raise HTTPException(502, f"Live quick signal could not load: {type(exc).__name__}: {exc}") from exc

    @app.post("/api/analyze/deep")
    def deep_analyze(request: DeepAnalyzeRequest):
        return deep_cache.get_or_run(
            request.market.value, request.symbol,
            lambda: run_deep_analysis(request), refresh=request.refresh)

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
