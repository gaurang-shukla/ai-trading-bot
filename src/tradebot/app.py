import importlib.util
import os
import secrets
import webbrowser
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters import CachedSignalProvider, PaperclipReporter, TradingAgentsClient
from .config import load_project_env
from .execution import PaperBroker
from .models import MarketKind, MarketSelection
from .overview import market_overview
from .risk import RiskEngine, RiskLimits
from .service import TradingService
from .venues import default_registry


WEB_DIR = Path(__file__).with_name("web")


load_project_env()
signals = CachedSignalProvider(TradingAgentsClient())


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._:/-]+$")
    market: MarketKind = MarketKind.CRYPTO_FUTURES
    venue: str = Field(default="weex", min_length=1, max_length=32)
    as_of: str = Field(default_factory=lambda: date.today().isoformat())
    equity: float = Field(default=100_000, gt=0, le=100_000_000)


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


def create_app() -> FastAPI:
    app = FastAPI(title="Signal", version="0.4.0")
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

    @app.get("/api/markets")
    def markets():
        return default_registry().choices()

    @app.get("/api/overview/{market}")
    def overview(market: MarketKind):
        try:
            return market_overview(market)
        except Exception as exc:
            raise HTTPException(502, f"Market overview could not load: {exc}") from exc

    def run_analysis(request: AnalyzeRequest):
        try:
            selection = MarketSelection(request.market, request.venue, request.symbol.upper())
            data = default_registry().market_data(selection)
            service = TradingService(
                data,
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
        return run_analysis(request)

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
        return {"paperclip_run_id": payload.runId, "result": run_analysis(request)}

    @app.get("/{path:path}")
    def app_route(path: str):
        if path.startswith("api/"):
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
