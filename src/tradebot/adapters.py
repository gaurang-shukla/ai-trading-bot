import json
import os
from datetime import date, datetime, timezone
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import MarketSnapshot, Side, TradeSignal


class MarketData(Protocol):
    def snapshot(self, symbol: str) -> MarketSnapshot: ...


class SignalProvider(Protocol):
    def analyze(self, symbol: str, as_of: str) -> TradeSignal: ...


class OpenBBClient:
    """OpenBB remains a separate service; this adapter consumes its public API."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("OPENBB_API_URL", "http://127.0.0.1:6900")).rstrip("/")

    def snapshot(self, symbol: str) -> MarketSnapshot:
        url = f"{self.base_url}/api/v1/equity/price/quote?symbol={symbol}"
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
        row = payload["results"][0]
        price = row.get("last_price") or row.get("price") or row.get("close")
        return MarketSnapshot(symbol.upper(), float(price), row.get("last_trade_timestamp", date.today().isoformat()), "openbb")


class _WeexPublicClient:
    base_url: str

    def _get(self, path: str, params: dict[str, str]) -> dict | list:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "three-layer-tradebot/0.2"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    @staticmethod
    def _timestamp(value: int | str | None) -> str:
        if value is None:
            return datetime.now(timezone.utc).isoformat()
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()


class WeexSpotMarketData(_WeexPublicClient):
    base_url = "https://api-spot.weex.com"

    def snapshot(self, symbol: str) -> MarketSnapshot:
        payload = self._get("/api/v3/market/ticker/24hr", {"symbol": symbol.upper()})
        row = payload[0] if isinstance(payload, list) else payload
        return MarketSnapshot(symbol.upper(), float(row["lastPrice"]),
                              self._timestamp(row.get("closeTime")), "weex_spot_v3")

    def tickers(self) -> list[dict]:
        payload = self._get("/api/v3/market/ticker/24hr", {})
        return payload if isinstance(payload, list) else [payload]


class WeexFuturesMarketData(_WeexPublicClient):
    base_url = "https://api-contract.weex.com"

    def snapshot(self, symbol: str) -> MarketSnapshot:
        row = self._get("/capi/v3/market/symbolPrice",
                        {"symbol": symbol.upper(), "priceType": "MARK"})
        return MarketSnapshot(symbol.upper(), float(row["price"]),
                              self._timestamp(row.get("time")), "weex_futures_v3_mark")

    def supported_symbols(self) -> list[str]:
        payload = self._get("/capi/v3/market/apiTradingSymbols", {})
        return [str(symbol) for symbol in payload]

    def tickers(self) -> list[dict]:
        payload = self._get("/capi/v3/market/ticker/24hr", {})
        return payload if isinstance(payload, list) else [payload]


class TradingAgentsClient:
    def __init__(self, config: dict | None = None):
        self.config = config

    def analyze(self, symbol: str, as_of: str) -> TradeSignal:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph(debug=False, config=self.config or DEFAULT_CONFIG.copy())
        _, decision = graph.propagate(symbol, as_of)
        raw = str(decision).upper()
        side = Side.BUY if "BUY" in raw else Side.SELL if "SELL" in raw else Side.HOLD
        # Upstream currently returns a decision, not a calibrated probability.
        confidence = 0.75 if side is not Side.HOLD else 0.0
        return TradeSignal(symbol.upper(), side, confidence, str(decision), "TradingAgents")


class PaperclipReporter:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.getenv("PAPERCLIP_API_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("PAPERCLIP_API_KEY", "")

    def report(self, event: dict) -> None:
        """Send an event to an explicitly configured task-bridge endpoint."""
        endpoint = os.getenv("PAPERCLIP_TASK_BRIDGE_URL", "")
        if not endpoint:
            return
        body = json.dumps(event).encode()
        request = Request(endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urlopen(request, timeout=10):
            pass

    @property
    def configured(self) -> bool:
        return bool(os.getenv("PAPERCLIP_TASK_BRIDGE_URL", "") and self.api_key)
