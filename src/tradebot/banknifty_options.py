"""Real-data-only Bank Nifty option-chain retrieval, normalization, and scoring."""

from dataclasses import asdict
from datetime import date, datetime, timezone
from io import StringIO
import csv
import importlib
import importlib.util
import math
import os
import threading
import time
from typing import Any, Protocol

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


class OptionChainProvider(Protocol):
    """Normalized, read-only option-chain provider contract."""

    name: str

    def is_configured(self) -> bool: ...
    def option_chain(self, underlying: str, expiry: str | None = None,
                     refresh: bool = False) -> dict: ...


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
    contracts, seen = [], set()
    for row in rows:
        strike, kind = _float(row.get("strike")), str(row.get("option_type") or "").upper()
        kind = {"CALL": "CE", "PUT": "PE", "C": "CE", "P": "PE"}.get(kind, kind)
        contract_expiry = str(row.get("expiry") or row.get("expiration") or "")
        numeric = {name: _float(row.get(name)) for name in (
            "last_price", "change", "volume", "open_interest", "delta", "gamma",
            "theta", "vega", "bid", "ask")}
        numeric["implied_volatility"] = _float(row.get("implied_volatility") if row.get("implied_volatility") is not None else row.get("iv"))
        key = (contract_expiry, strike, kind)
        invalid_number = any(value is not None and not math.isfinite(value) for value in numeric.values())
        invalid_quote = (numeric["volume"] is not None and numeric["volume"] < 0) or (numeric["open_interest"] is not None and numeric["open_interest"] < 0) or (numeric["bid"] is not None and numeric["bid"] < 0) or (numeric["ask"] is not None and numeric["ask"] < 0) or (numeric["bid"] is not None and numeric["ask"] is not None and numeric["bid"] > numeric["ask"])
        if (strike is None or strike <= 0 or kind not in {"CE", "PE"} or not contract_expiry
                or not math.isfinite(spot) or spot <= 0 or invalid_number or invalid_quote or key in seen):
            continue
        classification = moneyness(kind, strike, spot, atm)
        if expiry and contract_expiry != expiry or option_type and kind != option_type.upper() or money and classification != money.upper(): continue
        seen.add(key)
        contract = OptionContract(contract_expiry, strike, kind, numeric["last_price"],
            numeric["change"], numeric["volume"], numeric["open_interest"], numeric["implied_volatility"],
            numeric["delta"], numeric["gamma"], numeric["theta"], numeric["vega"],
            numeric["bid"], numeric["ask"], spot, classification, round((strike - spot) / spot * 100, 4))
        contracts.append({**asdict(contract), "score": option_score(contract)})
    expiries = sorted({str(row.get("expiry") or row.get("expiration")) for row in rows if row.get("expiry") or row.get("expiration")})
    return {"available": bool(contracts), "symbol": "BANKNIFTY", "underlying_symbol": "^NSEBANK",
            "underlying_price": spot, "atm_strike": atm, "expiries": expiries, "contracts": contracts,
            "source": raw.get("source", "OpenBB"), "research_only": True}


def _float(value) -> float | None:
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None


class DhanOptionChainProvider:
    """Authenticated DhanHQ market-data client. It has no order methods."""

    name = "DhanHQ"
    INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

    def __init__(self, client_id: str | None = None, access_token: str | None = None,
                 base_url: str | None = None, transport=None, retries: int = 2,
                 sleep=time.sleep, clock=time.time):
        self.client_id = client_id if client_id is not None else os.getenv("DHAN_CLIENT_ID", "")
        self.access_token = access_token if access_token is not None else os.getenv("DHAN_ACCESS_TOKEN", "")
        self.base_url = (base_url or os.getenv("DHAN_API_BASE_URL", "https://api.dhan.co/v2")).rstrip("/")
        self.transport = transport or requests.Session()
        self.retries, self.sleep, self.clock = max(0, min(retries, 3)), sleep, clock
        self.timeout = (float(os.getenv("DHAN_CONNECT_TIMEOUT_SECONDS", "3")),
                        float(os.getenv("DHAN_RESPONSE_TIMEOUT_SECONDS", "8")))
        self._cache: dict[str, tuple[float, Any]] = {}
        self._good: dict[str, dict] = {}
        self._lock = threading.RLock()

    def is_configured(self) -> bool:
        return bool(self.client_id.strip() and self.access_token.strip())

    def _request(self, method: str, url: str, **kwargs):
        if not self.is_configured():
            raise OptionChainError("dhan_not_configured", "DhanHQ credentials are not configured")
        headers = {"client-id": self.client_id, "access-token": self.access_token,
                   "Content-Type": "application/json", "Accept": "application/json"}
        for attempt in range(self.retries + 1):
            try:
                response = self.transport.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
            except (requests.Timeout, httpx.TimeoutException) as exc:
                category = "dhan_timeout"
                if attempt < self.retries:
                    self.sleep(min(2.0, .25 * 2 ** attempt)); continue
                raise OptionChainError(category, "DhanHQ request timed out") from exc
            except (requests.ConnectionError, httpx.HTTPError, OSError) as exc:
                if attempt < self.retries:
                    self.sleep(min(2.0, .25 * 2 ** attempt)); continue
                raise OptionChainError("dhan_connection_error", "Could not connect to DhanHQ") from exc
            status = response.status_code
            if status in {401, 403}:
                category = "dhan_authentication_failed" if status == 401 else "dhan_permission_denied"
                raise OptionChainError(category, "DhanHQ rejected market-data access")
            if status == 429 or 500 <= status < 600:
                if attempt < self.retries:
                    retry_after = response.headers.get("Retry-After", "")
                    try: delay = float(retry_after)
                    except (TypeError, ValueError): delay = .25 * 2 ** attempt
                    self.sleep(max(0, min(delay, 2.0))); continue
                category = "dhan_rate_limited" if status == 429 else "dhan_provider_error"
                raise OptionChainError(category, "DhanHQ is temporarily unavailable")
            if status >= 400:
                category = "dhan_invalid_instrument" if status == 400 else "dhan_provider_error"
                raise OptionChainError(category, "DhanHQ rejected the market-data request")
            try: return response.json()
            except (ValueError, requests.exceptions.JSONDecodeError) as exc:
                raise OptionChainError("dhan_invalid_response", "DhanHQ returned invalid data") from exc
        raise OptionChainError("dhan_provider_error", "DhanHQ request failed")

    def _instrument(self, refresh: bool = False) -> dict:
        with self._lock:
            cached = self._cache.get("instrument")
            if cached and not refresh and self.clock() - cached[0] < 86400:
                return cached[1]
        # Dhan publishes the master as CSV. Authentication is still required before use.
        if not self.is_configured():
            raise OptionChainError("dhan_not_configured", "DhanHQ credentials are not configured")
        try:
            response = self.transport.request("GET", self.INSTRUMENT_MASTER_URL,
                                              timeout=self.timeout, headers={"Accept": "text/csv"})
            if response.status_code >= 400: raise ValueError("instrument master unavailable")
            rows = list(csv.DictReader(StringIO(response.text)))
        except Exception as exc:
            raise OptionChainError("dhan_invalid_instrument", "DhanHQ instrument master is unavailable") from exc
        matches = []
        for row in rows:
            normalized = {str(k).strip().upper(): str(v or "").strip() for k, v in row.items()}
            symbol = normalized.get("SYMBOL_NAME") or normalized.get("DISPLAY_NAME") or normalized.get("TRADING_SYMBOL")
            exchange = normalized.get("EXCH_ID") or normalized.get("EXCHANGE_ID")
            segment = normalized.get("SEGMENT") or normalized.get("SEGMENT_NAME")
            instrument = normalized.get("INSTRUMENT") or normalized.get("INSTRUMENT_TYPE")
            security_id = normalized.get("SECURITY_ID") or normalized.get("SEM_SMST_SECURITY_ID")
            kind = (segment + instrument).upper()
            if symbol.upper().replace(" ", "") in {"BANKNIFTY", "NIFTYBANK"} and exchange.upper() == "NSE" and ("IDX" in kind or "INDEX" in kind) and security_id:
                matches.append({"security_id": security_id, "exchange_segment": "IDX_I", "symbol": "BANKNIFTY"})
        unique = {item["security_id"]: item for item in matches}
        if len(unique) != 1:
            raise OptionChainError("dhan_invalid_instrument", "BANKNIFTY instrument could not be resolved unambiguously")
        result = next(iter(unique.values()))
        with self._lock: self._cache["instrument"] = (self.clock(), result)
        return result

    @staticmethod
    def _expiry_values(payload: Any) -> list[str]:
        values = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(values, dict): values = values.get("data") or values.get("expiryList") or values.get("expiry") or []
        today, valid = date.today(), set()
        for value in values if isinstance(values, list) else []:
            try:
                parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
                if parsed >= today: valid.add(parsed.isoformat())
            except ValueError: pass
        return sorted(valid)

    def expiries(self, refresh: bool = False) -> list[str]:
        instrument = self._instrument(refresh)
        key = "expiries"
        with self._lock:
            cached = self._cache.get(key)
            if cached and not refresh and self.clock() - cached[0] < 300: return cached[1]
        payload = self._request("POST", f"{self.base_url}/optionchain/expirylist", json={
            "UnderlyingScrip": int(instrument["security_id"]), "UnderlyingSeg": instrument["exchange_segment"]})
        values = self._expiry_values(payload)
        if not values: raise OptionChainError("dhan_invalid_expiry", "DhanHQ returned no valid expiries")
        with self._lock: self._cache[key] = (self.clock(), values)
        return values

    def option_chain(self, underlying: str, expiry: str | None = None, refresh: bool = False) -> dict:
        if underlying.upper().replace(" ", "") != "BANKNIFTY":
            raise OptionChainError("dhan_invalid_instrument", "DhanHQ provider only supports BANKNIFTY here")
        instrument = self._instrument(refresh)
        expiries = self.expiries(refresh)
        chosen = expiry or expiries[0]
        if chosen not in expiries:
            expiries = self.expiries(True)
            if chosen not in expiries: raise OptionChainError("dhan_invalid_expiry", "Requested expiry is unavailable")
        key = f"chain:{chosen}"
        with self._lock:
            cached = self._cache.get(key)
            if cached and not refresh and self.clock() - cached[0] < 20: return dict(cached[1])
        body = {"UnderlyingScrip": int(instrument["security_id"]),
                "UnderlyingSeg": instrument["exchange_segment"], "Expiry": chosen}
        try:
            payload = self._request("POST", f"{self.base_url}/optionchain", json=body)
        except OptionChainError as exc:
            if exc.category == "dhan_invalid_instrument" and not refresh:
                self._instrument(True)
                return self.option_chain(underlying, chosen, True)
            good = self._good.get(key)
            if good:
                stale = dict(good); stale.update(stale=True, provider_status="stale", failure_category=exc.category)
                return stale
            raise
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        spot = _float(data.get("last_price") or data.get("underlyingPrice") or data.get("underlying_price"))
        chain = data.get("oc") or data.get("optionChain") or data.get("chain") or {}
        rows = []
        iterable = chain.items() if isinstance(chain, dict) else enumerate(chain if isinstance(chain, list) else [])
        for strike_key, item in iterable:
            if not isinstance(item, dict): continue
            strike = item.get("strike_price") or item.get("strikePrice") or strike_key
            for kind, aliases in (("CE", ("ce", "CE", "call")), ("PE", ("pe", "PE", "put"))):
                leg = next((item.get(alias) for alias in aliases if isinstance(item.get(alias), dict)), None)
                if not leg: continue
                greeks = leg.get("greeks") if isinstance(leg.get("greeks"), dict) else {}
                rows.append({"expiry": chosen, "strike": strike, "option_type": kind,
                    "last_price": leg.get("last_price", leg.get("ltp")), "change": leg.get("change"),
                    "volume": leg.get("volume"), "open_interest": leg.get("oi"),
                    "implied_volatility": leg.get("implied_volatility", leg.get("iv")),
                    "delta": greeks.get("delta", leg.get("delta")), "gamma": greeks.get("gamma", leg.get("gamma")),
                    "theta": greeks.get("theta", leg.get("theta")), "vega": greeks.get("vega", leg.get("vega")),
                    "bid": leg.get("top_bid_price", leg.get("bid")), "ask": leg.get("top_ask_price", leg.get("ask"))})
        raw = {"source": "DhanHQ", "contracts": rows, "underlying_price": spot,
               "expiries": expiries, "provider_timestamp": data.get("timestamp") or payload.get("timestamp")}
        normalized = build_chain(raw, spot or 0)
        if not normalized["contracts"]: raise OptionChainError("dhan_empty_chain", "DhanHQ returned no valid option contracts")
        now = datetime.now(timezone.utc).isoformat()
        raw.update(provider_status="connected", stale=False, application_refreshed_at=now)
        with self._lock:
            self._cache[key] = (self.clock(), raw); self._good[key] = dict(raw)
        return raw


class OpenBBOptionChainProvider:
    name = "OpenBB"
    def __init__(self, client_factory=None): self.client_factory = client_factory
    def is_configured(self) -> bool: return bool(os.getenv("OPENBB_API_URL"))
    def option_chain(self, underlying: str, expiry: str | None = None, refresh: bool = False) -> dict:
        from .adapters import OpenBBClient
        provider = self.client_factory() if self.client_factory else OpenBBClient(asset_class="index")
        raw = provider.option_chain(underlying, expiry)
        raw["underlying_price"] = provider.snapshot("^NSEBANK").price
        raw["source"] = self.name
        return raw


class NSEOptionChainProvider:
    name = "NSE"
    def __init__(self, client_factory=None): self.client_factory = client_factory
    def is_configured(self) -> bool: return True
    def option_chain(self, underlying: str, expiry: str | None = None, refresh: bool = False) -> dict:
        return (self.client_factory() if self.client_factory else NSEOptionChainClient()).option_chain(expiry)


class OptionChainService:
    """Provider registry with safe fallback and normalized output."""
    def __init__(self, providers: list[OptionChainProvider] | None = None):
        self.providers = providers or [DhanOptionChainProvider(), OpenBBOptionChainProvider(), NSEOptionChainProvider()]

    def option_chain(self, underlying="BANKNIFTY", expiry=None, refresh=False,
                     option_type=None, money=None) -> dict:
        attempts = []
        for provider in self.providers:
            configured = provider.is_configured()
            attempt = {"provider": provider.name, "attempted": configured, "failure_category": None}
            if not configured:
                attempt["failure_category"] = "dhan_not_configured" if provider.name == "DhanHQ" else "not_configured"
                attempts.append(attempt); continue
            try:
                raw = provider.option_chain(underlying, expiry, refresh)
                result = build_chain(raw, _float(raw.get("underlying_price")) or 0, expiry, option_type, money)
                if not build_chain(raw, _float(raw.get("underlying_price")) or 0)["contracts"]:
                    raise OptionChainError("empty_chain", "Provider returned no valid contracts")
                now = datetime.now(timezone.utc).isoformat()
                result.update(provider=provider.name, provider_status=raw.get("provider_status", "connected"),
                              stale=bool(raw.get("stale")), provider_timestamp=raw.get("provider_timestamp"),
                              application_refreshed_at=raw.get("application_refreshed_at", now),
                              cache_age_seconds=raw.get("cache_age_seconds", 0), provider_attempts=attempts + [attempt])
                return result
            except Exception as exc:
                category = getattr(exc, "category", "provider_error")
                if category == "empty_chain":
                    category = "nse_empty_chain" if provider.name == "NSE" else "openbb_empty_chain"
                if provider.name == "NSE" and category == "provider_error":
                    category = "nse_empty_chain" if "empty" in str(exc).lower() or "no valid" in str(exc).lower() else "nse_http_error"
                elif provider.name == "OpenBB" and category == "provider_error":
                    category = classify_openbb_failure(exc)
                attempt["failure_category"] = category
                attempts.append(attempt)
        return {"available": False, "message": UNAVAILABLE_MESSAGE, "symbol": underlying,
                "contracts": [], "expiries": [], "research_only": True, "provider": None,
                "provider_status": "temporarily_unavailable", "stale": False,
                "application_refreshed_at": datetime.now(timezone.utc).isoformat(),
                "provider_attempts": attempts}
