"""Real-data-only Bank Nifty option-chain retrieval, normalization, and scoring."""

from dataclasses import asdict
import importlib
import importlib.util
import os
import time
from typing import Any

import httpx

# ``requests`` is a declared runtime dependency.  The small httpx compatibility
# adapter keeps source-tree diagnostics usable in minimal development images
# before the project dependencies have been installed.
if importlib.util.find_spec("requests"):
    requests = importlib.import_module("requests")
else:
    class _Session:
        def __init__(self):
            self._client = httpx.Client(follow_redirects=True)
            self.headers = self._client.headers

        def get(self, url, timeout=None, allow_redirects=True, **kwargs):
            return self._client.get(url, timeout=timeout, **kwargs)

    class _Exceptions:
        JSONDecodeError = ValueError

    class _RequestsCompat:
        Session = _Session
        RequestException = httpx.HTTPError
        Timeout = httpx.TimeoutException
        ConnectionError = httpx.ConnectError
        exceptions = _Exceptions()

    requests = _RequestsCompat()

from .models import OptionContract

UNAVAILABLE_MESSAGE = "Real Bank Nifty option-chain data is temporarily unavailable."
RETRY_STATUSES = {401, 403, 429, 500, 502, 503, 504}


def empty_provider_diagnostic(provider: str) -> dict[str, Any]:
    return {"provider": provider, "attempted": False, "status_code": None,
            "final_url": None, "content_type": None, "got_json": False,
            "raw_row_count": 0, "ce_count": 0, "pe_count": 0,
            "normalized_contract_count": 0, "failure_category": None,
            "sanitized_error": None}


class OptionChainError(RuntimeError):
    """Provider failure carrying safe, structured diagnostics."""

    def __init__(self, category: str, message: str, diagnostic: dict | None = None):
        super().__init__(message)
        self.category = category
        self.diagnostic = diagnostic or {}


class NSEOptionChainClient:
    """Cookie-aware client for NSE's public BANKNIFTY index-chain endpoint."""

    BASE_URL = "https://www.nseindia.com"
    HEADERS = {
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.nseindia.com/option-chain",
        "connection": "keep-alive",
        "cache-control": "no-cache",
    }

    def __init__(self, timeout: float | None = None, retries: int = 2,
                 session: requests.Session | None = None):
        self.timeout = timeout if timeout is not None else float(os.getenv("NSE_TIMEOUT_SECONDS", "8"))
        self.retries = max(0, retries)
        self.session = session or requests.Session()
        self.session.headers.update(self.HEADERS)
        self.diagnostic = empty_provider_diagnostic("nse")
        self.initial_status_code: int | None = None
        self.top_level_json_keys: list[str] = []

    def option_chain(self, expiry: str | None = None) -> dict:
        self.diagnostic = empty_provider_diagnostic("nse")
        self.diagnostic["attempted"] = True
        self.initial_status_code = None
        self.top_level_json_keys = []
        try:
            initial = self._request(f"{self.BASE_URL}/", initial=True)
            self.initial_status_code = initial.status_code
            response = self._request(f"{self.BASE_URL}/api/option-chain-indices", params={"symbol": "BANKNIFTY"})
            self.diagnostic.update(status_code=response.status_code, final_url=response.url,
                                   content_type=response.headers.get("content-type", ""))
            try:
                payload = response.json()
                self.diagnostic["got_json"] = True
                self.top_level_json_keys = sorted(payload) if isinstance(payload, dict) else []
            except (requests.exceptions.JSONDecodeError, ValueError) as exc:
                self._fail("nse_html_instead_of_json", "NSE returned non-JSON content", exc)
            records = payload.get("records") if isinstance(payload, dict) else None
            data = records.get("data") if isinstance(records, dict) else None
            data = data if isinstance(data, list) else []
            self.diagnostic["raw_row_count"] = len(data)
            rows = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                row_expiry = str(item.get("expiryDate") or "")
                if expiry and row_expiry != expiry:
                    continue
                for kind in ("CE", "PE"):
                    leg = item.get(kind)
                    if not isinstance(leg, dict):
                        continue
                    self.diagnostic[kind.lower() + "_count"] += 1
                    rows.append({"expiry": row_expiry, "strike": item.get("strikePrice"),
                        "option_type": kind, "last_price": leg.get("lastPrice"),
                        "change": leg.get("change"), "volume": leg.get("totalTradedVolume"),
                        "open_interest": leg.get("openInterest"),
                        "implied_volatility": leg.get("impliedVolatility"),
                        "bid": leg.get("bidprice"), "ask": leg.get("askPrice")})
            spot = records.get("underlyingValue") if isinstance(records, dict) else None
            if spot is None:
                spot = next((leg.get("underlyingValue") for item in data
                             for leg in (item.get("CE"), item.get("PE"))
                             if isinstance(leg, dict) and leg.get("underlyingValue") is not None), None)
            raw = {"symbol": "BANKNIFTY", "source": "NSE", "contracts": rows,
                   "expiries": records.get("expiryDates") or [], "underlying_price": _float(spot)}
            valid = build_chain(raw, _float(spot) or 0)["contracts"] if spot is not None else []
            self.diagnostic["normalized_contract_count"] = len(valid)
            if not rows or not valid or spot is None:
                self._fail("nse_empty_chain", "NSE returned JSON but no valid CE/PE option rows")
            return raw
        except OptionChainError:
            raise
        except requests.RequestException as exc:
            self._fail("nse_http_error", "NSE request failed", exc)

    def _request(self, url: str, initial: bool = False, **kwargs):
        response = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=True, **kwargs)
                if initial:
                    self.initial_status_code = response.status_code
                if response.status_code not in RETRY_STATUSES:
                    response.raise_for_status()
                    return response
                if attempt < self.retries:
                    time.sleep(.25 * (attempt + 1))
                    continue
                category = "nse_blocked_by_provider" if response.status_code in {401, 403, 429} else "nse_http_error"
                self.diagnostic.update(status_code=response.status_code, final_url=response.url,
                                       content_type=response.headers.get("content-type", ""))
                self._fail(category, f"NSE returned HTTP {response.status_code}")
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < self.retries:
                    time.sleep(.25 * (attempt + 1))
                    continue
                self._fail("nse_http_error", "NSE request timed out or could not connect", exc)
        return response

    def _fail(self, category: str, message: str, exc: Exception | None = None):
        self.diagnostic["failure_category"] = category
        self.diagnostic["sanitized_error"] = message
        raise OptionChainError(category, message, self.diagnostic.copy()) from exc


def classify_openbb_failure(exc: Exception | None = None, empty: bool = False) -> str:
    """Classify the local OpenBB REST-service result without claiming symbol support."""
    if empty:
        return "openbb_empty_chain"
    text = f"{type(exc).__name__}: {exc}".lower()
    if "connection refused" in text or "errno 61" in text or "errno 111" in text:
        return "openbb_connection_refused"
    if "unsupported" in text or "not supported" in text or "404" in text:
        return "openbb_option_chain_unsupported"
    return "openbb_provider_error"


def atm_strike(strikes: list[float], spot: float) -> float | None:
    return min(strikes, key=lambda strike: (abs(strike - spot), strike)) if strikes else None


def moneyness(option_type: str, strike: float, spot: float, atm: float) -> str:
    if strike == atm: return "ATM"
    if option_type == "CE": return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def option_score(contract: OptionContract) -> dict:
    liquid = (contract.volume or 0) > 0 and (contract.open_interest or 0) > 0
    spread_pct = ((contract.ask - contract.bid) / contract.last_price * 100
                  if contract.ask is not None and contract.bid is not None and contract.last_price else None)
    risk = min(100, 35 + (25 if not liquid else 0) +
               (20 if spread_pct is not None and spread_pct > 5 else 0) +
               (10 if contract.moneyness == "OTM" else 0))
    momentum = contract.change or 0
    if not liquid or risk >= 75:
        signal, confidence, reason = "AVOID", .82, "Insufficient liquidity or an excessive quoted spread."
    elif contract.moneyness == "ATM" and momentum > 0:
        signal, confidence, reason = f"BUY {contract.option_type}", min(.9, .62 + min(momentum, 10) / 50), "ATM contract has positive price momentum and observable liquidity."
    else: signal, confidence, reason = "WATCH", .58, "No strong, liquid ATM momentum setup is present."
    price = contract.last_price
    return {"signal": signal, "confidence": round(confidence, 2), "risk_score": risk, "reason": reason,
            "suggested_stop_loss": round(price * .8, 2) if price is not None else None,
            "suggested_target": round(price * 1.3, 2) if price is not None else None}


def build_chain(raw: dict, spot: float, expiry: str | None = None,
                option_type: str | None = None, money: str | None = None) -> dict:
    rows = raw.get("contracts") or []
    strikes = sorted({value for row in rows if (value := _float(row.get("strike"))) is not None and value > 0})
    atm = atm_strike(strikes, spot)
    contracts = []
    for row in rows:
        strike, kind = _float(row.get("strike")), str(row.get("option_type") or "").upper()
        kind = {"CALL": "CE", "PUT": "PE", "C": "CE", "P": "PE"}.get(kind, kind)
        contract_expiry = str(row.get("expiry") or row.get("expiration") or "")
        if strike is None or strike <= 0 or kind not in {"CE", "PE"} or not contract_expiry or spot <= 0: continue
        classification = moneyness(kind, strike, spot, atm)
        if expiry and contract_expiry != expiry or option_type and kind != option_type.upper() or money and classification != money.upper(): continue
        contract = OptionContract(contract_expiry, strike, kind, _float(row.get("last_price")),
            _float(row.get("change")), _float(row.get("volume")), _float(row.get("open_interest")),
            _float(row.get("implied_volatility") if row.get("implied_volatility") is not None else row.get("iv")),
            _float(row.get("delta")), _float(row.get("gamma")), _float(row.get("theta")), _float(row.get("vega")),
            _float(row.get("bid")), _float(row.get("ask")), spot, classification, round((strike - spot) / spot * 100, 4))
        contracts.append({**asdict(contract), "score": option_score(contract)})
    expiries = sorted({str(row.get("expiry") or row.get("expiration")) for row in rows if row.get("expiry") or row.get("expiration")})
    return {"available": bool(contracts), "symbol": "BANKNIFTY", "underlying_symbol": "^NSEBANK",
            "underlying_price": spot, "atm_strike": atm, "expiries": expiries, "contracts": contracts,
            "source": raw.get("source", "OpenBB"), "research_only": True}


def _float(value) -> float | None:
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None
