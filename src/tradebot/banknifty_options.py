"""Real-data-only Bank Nifty option-chain normalization and scoring."""

from dataclasses import asdict

from .models import OptionContract

UNAVAILABLE_MESSAGE = "Bank Nifty options data provider not configured yet."


def atm_strike(strikes: list[float], spot: float) -> float | None:
    """Return the listed strike nearest spot; lower strike wins an exact tie."""
    return min(strikes, key=lambda strike: (abs(strike - spot), strike)) if strikes else None


def moneyness(option_type: str, strike: float, spot: float, atm: float) -> str:
    if strike == atm:
        return "ATM"
    if option_type == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def option_score(contract: OptionContract) -> dict:
    """Score only observable contract properties with deterministic rules."""
    liquid = (contract.volume or 0) > 0 and (contract.open_interest or 0) > 0
    spread_pct = ((contract.ask - contract.bid) / contract.last_price * 100
                  if contract.ask is not None and contract.bid is not None
                  and contract.last_price else None)
    risk = 35 + (25 if not liquid else 0) + (20 if spread_pct is not None and spread_pct > 5 else 0)
    risk = min(100, risk + (10 if contract.moneyness == "OTM" else 0))
    momentum = contract.change or 0
    if not liquid or risk >= 75:
        signal, confidence, reason = "AVOID", .82, "Insufficient liquidity or an excessive quoted spread."
    elif contract.moneyness == "ATM" and momentum > 0:
        signal, confidence = f"BUY {contract.option_type}", min(.9, .62 + min(momentum, 10) / 50)
        reason = "ATM contract has positive price momentum and observable liquidity."
    else:
        signal, confidence, reason = "WATCH", .58, "No strong, liquid ATM momentum setup is present."
    price = contract.last_price
    return {"signal": signal, "confidence": round(confidence, 2), "risk_score": risk,
            "reason": reason,
            "suggested_stop_loss": round(price * .8, 2) if price is not None else None,
            "suggested_target": round(price * 1.3, 2) if price is not None else None}


def build_chain(raw: dict, spot: float, expiry: str | None = None,
                option_type: str | None = None, money: str | None = None) -> dict:
    rows = raw.get("contracts") or []
    strikes = sorted({float(row["strike"]) for row in rows if row.get("strike") is not None})
    atm = atm_strike(strikes, spot)
    contracts = []
    for row in rows:
        strike = _float(row.get("strike"))
        kind = str(row.get("option_type") or "").upper()
        kind = {"CALL": "CE", "PUT": "PE", "C": "CE", "P": "PE"}.get(kind, kind)
        contract_expiry = str(row.get("expiry") or row.get("expiration") or "")
        if strike is None or kind not in {"CE", "PE"} or not contract_expiry:
            continue
        classification = moneyness(kind, strike, spot, atm)
        if expiry and contract_expiry != expiry:
            continue
        if option_type and kind != option_type.upper():
            continue
        if money and classification != money.upper():
            continue
        contract = OptionContract(
            contract_expiry, strike, kind, _float(row.get("last_price")),
            _float(row.get("change")), _float(row.get("volume")),
            _float(row.get("open_interest")),
            _float(row.get("implied_volatility") if row.get("implied_volatility") is not None else row.get("iv")),
            _float(row.get("delta")), _float(row.get("gamma")), _float(row.get("theta")),
            _float(row.get("vega")), _float(row.get("bid")), _float(row.get("ask")), spot,
            classification, round((strike - spot) / spot * 100, 4))
        contracts.append({**asdict(contract), "score": option_score(contract)})
    expiries = sorted({str(row.get("expiry") or row.get("expiration")) for row in rows
                       if row.get("expiry") or row.get("expiration")})
    return {"available": bool(contracts), "symbol": "BANKNIFTY", "underlying_symbol": "^NSEBANK",
            "underlying_price": spot, "atm_strike": atm, "expiries": expiries,
            "contracts": contracts, "source": "OpenBB", "research_only": True}


def _float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
