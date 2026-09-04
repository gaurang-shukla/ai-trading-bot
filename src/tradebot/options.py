"""Research-only Bank Nifty option-chain normalization and deterministic scoring."""

from __future__ import annotations

from dataclasses import asdict
from math import log10

from .models import IndexOption


def atm_strike(strikes: list[float], spot: float) -> float | None:
    """Return the listed strike nearest spot; never invent a strike."""
    return min(strikes, key=lambda strike: (abs(strike - spot), strike)) if strikes else None


def normalize_banknifty_option(row: dict, spot: float, atm: float | None = None) -> IndexOption | None:
    strike = row.get("strike")
    kind = str(row.get("option_type") or "").upper()
    if strike is None or kind not in {"CE", "PE"} or spot <= 0:
        return None
    strike = float(strike)
    atm = strike if atm is None else atm
    moneyness = ("ATM" if strike == atm else
                 "ITM" if (kind == "CE" and strike < atm) or (kind == "PE" and strike > atm)
                 else "OTM")
    return IndexOption(
        expiry=row.get("expiration"), strike=strike, option_type=kind,
        last_price=row.get("last_price"), change=row.get("change"), volume=row.get("volume"),
        open_interest=row.get("open_interest"), implied_volatility=row.get("iv"),
        delta=row.get("delta"), gamma=row.get("gamma"), theta=row.get("theta"), vega=row.get("vega"),
        bid=row.get("bid"), ask=row.get("ask"), underlying_price=spot, moneyness=moneyness,
        distance_from_spot_pct=round(abs(strike - spot) / spot * 100, 4))


def filter_options(rows: list[dict], option_type: str | None = None,
                   moneyness: str | None = None) -> list[dict]:
    option_type, moneyness = (option_type or "").upper(), (moneyness or "").upper()
    return [row for row in rows
            if (not option_type or row["option_type"] == option_type)
            and (not moneyness or row["moneyness"] == moneyness)]


def score_option(row: IndexOption, underlying_side: str) -> dict:
    """Rank a real contract using transparent market fields; never creates contracts."""
    aligned = (("BUY" in underlying_side and row.option_type == "CE") or
               ("SELL" in underlying_side and row.option_type == "PE"))
    neutral = not ("BUY" in underlying_side or "SELL" in underlying_side)
    liquidity = min(1.0, log10(1 + (row.open_interest or 0) + (row.volume or 0)) / 6)
    momentum = max(-1.0, min(1.0, (row.change or 0) / 10))
    distance_quality = max(0.0, 1 - row.distance_from_spot_pct / 5)
    iv_risk = min(1.0, max(0.0, (row.implied_volatility or 0) / 100))
    score = (.30 * (1 if aligned else -.35 if not neutral else 0) + .25 * liquidity +
             .18 * momentum + .17 * distance_quality - .10 * iv_risk)
    action = "WATCH" if neutral else "BUY CE" if score >= .35 and row.option_type == "CE" else \
        "BUY PE" if score >= .35 and row.option_type == "PE" else "AVOID"
    confidence = round(max(.5, min(.92, .55 + abs(score) * .35)), 3)
    risk = round(max(.1, min(1.0, .35 + iv_risk * .35 + row.distance_from_spot_pct / 20)), 3)
    price = row.last_price
    reason = (f"{row.moneyness} {row.option_type}, {row.distance_from_spot_pct:.2f}% from spot; "
              f"direction is {'aligned' if aligned else 'neutral' if neutral else 'not aligned'}, "
              f"with OI {row.open_interest or 0:g} and volume {row.volume or 0:g}.")
    return {"action": action, "confidence": confidence, "risk_score": risk, "reason": reason,
            "suggested_stop_loss": round(price * (1 - min(.4, .15 + risk * .15)), 2) if price else None,
            "suggested_target": round(price * (1 + max(.2, (1-risk) * .5)), 2) if price else None}


def prepare_chain(rows: list[dict], spot: float, underlying_side: str,
                  option_type: str | None = None, moneyness: str | None = None) -> dict:
    strikes = sorted({float(row["strike"]) for row in rows if row.get("strike") is not None})
    atm = atm_strike(strikes, spot)
    options = [item for row in rows if (item := normalize_banknifty_option(row, spot, atm))]
    contracts = [{**asdict(item), "signal": score_option(item, underlying_side)} for item in options]
    return {"atm_strike": atm, "contracts": filter_options(contracts, option_type, moneyness)}
