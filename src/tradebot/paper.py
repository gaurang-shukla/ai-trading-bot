"""Durable, local-only paper portfolio accounting.

This module deliberately has no dependency on an execution venue.  Prices are
passed in by the web service and every mutation is recorded in local SQLite.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SECRET_WORDS = ("api_key", "secret", "token", "password", "passphrase", "authorization")


def _safe_snapshot(value: Any) -> Any:
    """Copy JSON data while removing credentials accidentally supplied by a caller."""
    if isinstance(value, dict):
        return {str(key): _safe_snapshot(item) for key, item in value.items()
                if not any(word in str(key).lower() for word in _SECRET_WORDS)}
    if isinstance(value, (list, tuple)):
        return [_safe_snapshot(item) for item in value]
    return value


def _positive_number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a positive number")
    return result


class PaperStore:
    """Small SQLite repository for the paper account, positions and journal."""

    def __init__(self, path: str | Path | None = None, starting_cash: float | None = None):
        self.path = Path(path or os.getenv("SIGNAL_DB_PATH", "data/signal.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        configured_cash = starting_cash if starting_cash is not None else os.getenv("PAPER_STARTING_CASH", "100000")
        self.starting_cash = _positive_number(configured_cash, "PAPER_STARTING_CASH")
        if self.starting_cash > 1_000_000_000:
            raise ValueError("PAPER_STARTING_CASH must be a positive, sensible amount")
        self._lock = threading.RLock()
        self.recovered_database: Path | None = None
        try:
            self._initialize()
        except sqlite3.DatabaseError:
            # Never overwrite an unreadable portfolio. Quarantine it for manual
            # recovery and bring paper mode back with a clean, local database.
            if not self.path.exists():
                raise
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            self.path.replace(backup)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    sidecar.replace(Path(f"{backup}{suffix}"))
            self.recovered_database = backup
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _database(self):
        """Always commit/roll back and close SQLite handles, including on errors."""
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS paper_account (
          id INTEGER PRIMARY KEY CHECK(id=1), starting_balance REAL NOT NULL,
          cash_balance REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS paper_positions (
          id TEXT PRIMARY KEY, market TEXT NOT NULL, symbol TEXT NOT NULL,
          display_name TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
          entry_price REAL NOT NULL, current_price REAL NOT NULL, quantity REAL NOT NULL,
          notional_value REAL NOT NULL, stop_loss REAL, take_profit REAL, risk_score REAL,
          confidence REAL, position_size_pct REAL, opened_at TEXT NOT NULL,
          source_signal_action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
          signal_snapshot TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_trades (
          id TEXT PRIMARY KEY, position_id TEXT NOT NULL, market TEXT NOT NULL,
          symbol TEXT NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL,
          exit_price REAL NOT NULL, quantity REAL NOT NULL, realized_pnl REAL NOT NULL,
          realized_pnl_pct REAL NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT NOT NULL,
          close_reason TEXT NOT NULL, signal_snapshot TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watchlist_items (
          market TEXT NOT NULL, symbol TEXT NOT NULL, display_name TEXT NOT NULL,
          added_at TEXT NOT NULL, latest_action TEXT, latest_confidence REAL,
          latest_price REAL, PRIMARY KEY(market,symbol)
        );
        CREATE TABLE IF NOT EXISTS journal_notes (
          id TEXT PRIMARY KEY, position_id TEXT, symbol TEXT, note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
        with self._lock, self._database() as db:
            db.executescript(schema)
            # Additive migrations keep existing local paper portfolios intact.
            position_columns = {row[1] for row in db.execute("PRAGMA table_info(paper_positions)")}
            if "price_available" not in position_columns:
                db.execute("ALTER TABLE paper_positions ADD COLUMN price_available INTEGER NOT NULL DEFAULT 1")
            trade_columns = {row[1] for row in db.execute("PRAGMA table_info(paper_trades)")}
            if "entry_notional" not in trade_columns:
                db.execute("ALTER TABLE paper_trades ADD COLUMN entry_notional REAL")
            if "exit_value" not in trade_columns:
                db.execute("ALTER TABLE paper_trades ADD COLUMN exit_value REAL")
            for name in ("max_price", "min_price"):
                if name not in position_columns:
                    db.execute(f"ALTER TABLE paper_positions ADD COLUMN {name} REAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS paper_trade_reviews (
                  trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL,
                  side TEXT NOT NULL, result TEXT NOT NULL, close_reason TEXT NOT NULL,
                  realized_return_pct REAL NOT NULL, grade TEXT NOT NULL,
                  trap_category TEXT, lesson TEXT NOT NULL, review_json TEXT NOT NULL,
                  generated_at TEXT NOT NULL,
                  FOREIGN KEY(trade_id) REFERENCES paper_trades(id)
                )
            """)
            db.execute("INSERT OR IGNORE INTO paper_account VALUES (1,?,?,0)",
                       (self.starting_cash, self.starting_cash))
            db.execute("PRAGMA user_version=2")

    @staticmethod
    def pnl(side: str, entry: float, current: float, quantity: float) -> float:
        if side not in {"LONG", "SHORT"}:
            raise ValueError("Side must be LONG or SHORT")
        entry = _positive_number(entry, "Entry price")
        current = _positive_number(current, "Current price")
        quantity = _positive_number(quantity, "Quantity")
        return (current - entry) * quantity if side == "LONG" else (entry - current) * quantity

    def positions(self) -> list[dict[str, Any]]:
        with self._database() as db:
            rows = db.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
        return [self._position(row) for row in rows]

    def _position(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["signal_snapshot"] = json.loads(item["signal_snapshot"])
        except (TypeError, json.JSONDecodeError):
            item["signal_snapshot"] = {}
        profit = self.pnl(item["side"], item["entry_price"], item["current_price"], item["quantity"])
        item["unrealized_pnl"] = profit
        item["unrealized_pnl_pct"] = profit / item["notional_value"] * 100
        item["entry_notional"] = item["notional_value"]
        item["amount_invested"] = item["notional_value"]
        item["current_value"] = item["notional_value"] + profit
        item["allocated_pct"] = item["notional_value"] / self.starting_cash * 100
        item["position_status"] = self.position_status(item)
        return item

    @staticmethod
    def trigger_reason(position: dict[str, Any]) -> str | None:
        """Return the paper exit trigger crossed by the last real provider mark."""
        current, stop, target = position.get("current_price"), position.get("stop_loss"), position.get("take_profit")
        if current is None:
            return None
        if position["side"] == "LONG":
            if stop is not None and current <= stop:
                return "stop_loss"
            if target is not None and current >= target:
                return "take_profit"
        else:
            if stop is not None and current >= stop:
                return "stop_loss"
            if target is not None and current <= target:
                return "take_profit"
        return None

    @classmethod
    def position_status(cls, position: dict[str, Any]) -> str:
        if not bool(position.get("price_available", True)):
            return "Price unavailable"
        return {"stop_loss": "Stop loss breached", "take_profit": "Take profit reached"}.get(
            cls.trigger_reason(position), "Active")

    def mark(self, position_id: str, price: float) -> None:
        price = _positive_number(price, "Current price")
        with self._lock, self._database() as db:
            db.execute("UPDATE paper_positions SET current_price=?,price_available=1,"
                       "max_price=MAX(COALESCE(max_price,entry_price),?),"
                       "min_price=MIN(COALESCE(min_price,entry_price),?) "
                       "WHERE id=? AND status='open'", (price, price, price, position_id))

    def mark_unavailable(self, position_id: str) -> None:
        """Retain the last valid price while making its stale/unavailable state explicit."""
        with self._lock, self._database() as db:
            db.execute("UPDATE paper_positions SET price_available=0 WHERE id=? AND status='open'", (position_id,))

    def account(self) -> dict[str, Any]:
        positions = self.positions()
        unrealized = sum(item["unrealized_pnl"] for item in positions)
        with self._database() as db:
            account = dict(db.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            count, wins = db.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl>0),0) FROM paper_trades").fetchone()
        capital = sum(x["notional_value"] for x in positions)
        equity = account["cash_balance"] + sum(x["current_value"] for x in positions)
        return {"starting_balance": account["starting_balance"], "cash_balance": account["cash_balance"],
                "available_paper_cash": account["cash_balance"], "capital_in_open_trades": capital,
                "equity": equity, "realized_pnl": account["realized_pnl"], "unrealized_pnl": unrealized,
                "total_pnl": account["realized_pnl"] + unrealized,
                "win_rate": (wins / count * 100 if count else 0), "open_positions_count": len(positions),
                "closed_trades_count": count, "mode": "paper"}

    def open_position(self, *, market: str, symbol: str, display_name: str, side: str,
                      price: float, notional: float, signal: dict | str,
                      risk_plan: dict | None = None) -> dict:
        if side not in {"LONG", "SHORT"}:
            raise ValueError("Side must be LONG or SHORT")
        price = _positive_number(price, "Live price")
        notional = _positive_number(notional, "Notional amount")
        quantity = notional / price
        if not math.isfinite(quantity) or not (0 < quantity <= 1e18):
            raise ValueError("Calculated quantity is invalid")
        # Callers may provide the complete Quick Signal result.  Keep the legacy
        # signal-only contract working while producing one stable canonical snapshot.
        supplied = signal if isinstance(signal, dict) else {}
        is_quick = isinstance(supplied.get("signal"), (dict, str))
        quick = supplied if is_quick else {"signal": signal, "risk_plan": risk_plan}
        raw_signal = quick.get("signal")
        source_signal = raw_signal if isinstance(raw_signal, dict) else (
            {"side": raw_signal} if isinstance(raw_signal, str) else supplied
        )
        plan = quick.get("risk_plan") if isinstance(quick.get("risk_plan"), dict) else risk_plan
        plan = plan if isinstance(plan, dict) else {}
        levels = quick.get("key_levels") if isinstance(quick.get("key_levels"), dict) else {}
        volatility = quick.get("volatility_summary") if isinstance(quick.get("volatility_summary"), dict) else {}
        momentum = quick.get("momentum_summary") if isinstance(quick.get("momentum_summary"), dict) else {}
        snapshot = _safe_snapshot({
            "market": market, "symbol": symbol.upper(), "display_name": display_name,
            "side": side, "source": "Quick Signal", "signal": source_signal,
            "advanced_research_decision": quick.get("advanced_research_decision") or quick.get("deep_research"),
            "entry_price": price, "stop_loss": plan.get("stop_loss"),
            "take_profit": plan.get("take_profit"), "risk_score": plan.get("risk_score"),
            "confidence": source_signal.get("confidence", quick.get("confidence")),
            "opportunity_score": quick.get("opportunity_score"),
            "position_size_pct": plan.get("position_size_pct"), "amount_invested": notional,
            "estimated_quantity": quantity, "setup_refreshed_at": quick.get("setup_refreshed_at") or quick.get("last_updated"),
            "provider_data_timestamp": quick.get("provider_data_timestamp"),
            "chart_timeframe": quick.get("chart_default_timeframe"),
            "multi_timeframe_rows": quick.get("timeframe_breakdown") or [],
            "support_level": levels.get("support") or quick.get("support_level"),
            "resistance_level": levels.get("resistance") or quick.get("resistance_level"),
            "atr_pct": quick.get("atr_pct") or volatility.get("atr_pct"),
            "rsi_summary": quick.get("rsi_summary") or momentum.get("rsi"),
            "macd_summary": quick.get("macd_summary") or momentum.get("macd"),
            "ema_bias_summary": quick.get("ema_bias_summary") or quick.get("trend_summary"),
            "volume": quick.get("volume"), "change_24h": quick.get("change_24h"),
            "funding_rate": quick.get("funding_rate"), "volatility": quick.get("volatility") or volatility or None,
            "reason": source_signal.get("rationale") or quick.get("reasoning") or quick.get("plain_language_reason") or quick.get("reason"),
            "risk_plan": plan,
        })
        item = {"id": uuid.uuid4().hex, "market": market, "symbol": symbol.upper(),
                "display_name": display_name, "side": side, "entry_price": price,
                "current_price": price, "quantity": quantity, "notional_value": notional,
                "stop_loss": plan.get("stop_loss"), "take_profit": plan.get("take_profit"),
                "risk_score": plan.get("risk_score"), "confidence": source_signal.get("confidence", quick.get("confidence")),
                "position_size_pct": plan.get("position_size_pct"), "opened_at": _now(),
                "source_signal_action": str(source_signal.get("side", "HOLD")), "status": "open",
                "signal_snapshot": snapshot, "max_price": price, "min_price": price}
        with self._lock, self._database() as db:
            # BEGIN IMMEDIATE serializes the balance check across processes and
            # across multiple PaperStore instances, not only threads in this instance.
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT 1 FROM paper_positions WHERE market=? AND symbol=? AND status='open'",
                (market, item["symbol"]),
            ).fetchone()
            if duplicate:
                raise ValueError("An open paper position already exists for this asset")
            db.execute("UPDATE paper_account SET cash_balance=cash_balance-? WHERE id=1 AND cash_balance>=?",
                       (notional, notional))
            if not db.execute("SELECT changes()").fetchone()[0]:
                raise ValueError("Not enough paper cash")
            columns = ",".join(item)
            values = list(item.values())
            values[list(item).index("signal_snapshot")] = json.dumps(item["signal_snapshot"], default=str)
            db.execute(f"INSERT INTO paper_positions ({columns}) VALUES ({','.join('?' for _ in item)})", values)
        position_id = item["id"]
        return next(position for position in self.positions() if position["id"] == position_id)

    def close_position(self, position_id: str, price: float, reason: str = "manual") -> dict:
        price = _positive_number(price, "Live price")
        if not isinstance(reason, str):
            raise ValueError("Close reason must be text")
        reason = reason.strip().lower()
        if reason not in {"stop_loss", "take_profit", "manual"}:
            reason = "manual"
        with self._lock, self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM paper_positions WHERE id=? AND status='open'", (position_id,)).fetchone()
            if not row:
                raise KeyError(position_id)
            position = dict(row)
            profit = self.pnl(position["side"], position["entry_price"], price, position["quantity"])
            trade = {"id": uuid.uuid4().hex, "position_id": position_id, "market": position["market"],
                     "symbol": position["symbol"], "side": position["side"], "entry_price": position["entry_price"],
                     "exit_price": price, "quantity": position["quantity"], "realized_pnl": profit,
                     "realized_pnl_pct": profit / position["notional_value"] * 100,
                     "opened_at": position["opened_at"], "closed_at": _now(),
                     "close_reason": reason, "signal_snapshot": position["signal_snapshot"],
                     "entry_notional": position["notional_value"],
                     "exit_value": position["notional_value"] + profit}
            db.execute("UPDATE paper_positions SET status='closed',current_price=? WHERE id=?", (price, position_id))
            db.execute("UPDATE paper_account SET cash_balance=cash_balance+?, realized_pnl=realized_pnl+? WHERE id=1",
                       (position["notional_value"] + profit, profit))
            db.execute(f"INSERT INTO paper_trades ({','.join(trade)}) VALUES ({','.join('?' for _ in trade)})", list(trade.values()))
            review = self._build_review(trade, position)
            self._save_review(db, review)
        trade["signal_snapshot"] = json.loads(trade["signal_snapshot"])
        trade["result"] = "win" if profit > 0 else "loss" if profit < 0 else "breakeven"
        return trade

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    def _build_review(self, trade: dict, position: dict | None = None) -> dict:
        try:
            snapshot = json.loads(trade.get("signal_snapshot") or "{}") if isinstance(trade.get("signal_snapshot"), str) else (trade.get("signal_snapshot") or {})
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        pnl = float(trade["realized_pnl"])
        result = "win" if pnl > 1e-9 else "loss" if pnl < -1e-9 else "breakeven"
        entry, exit_price = float(trade["entry_price"]), float(trade["exit_price"])
        amount = self._number(trade.get("entry_notional")) or entry * float(trade["quantity"])
        return_pct = pnl / amount * 100 if amount else 0.0
        opened = datetime.fromisoformat(str(trade["opened_at"]).replace("Z", "+00:00"))
        closed = datetime.fromisoformat(str(trade["closed_at"]).replace("Z", "+00:00"))
        atr = self._number(snapshot.get("atr_pct"))
        change = self._number(snapshot.get("change_24h"))
        stop = self._number(snapshot.get("stop_loss"))
        resistance = self._number(snapshot.get("resistance_level"))
        support = self._number(snapshot.get("support_level"))
        rows = snapshot.get("multi_timeframe_rows") or []
        labels = [str(row.get("signal") or row.get("trend") or row.get("bias") or "").lower() for row in rows if isinstance(row, dict)]
        stale = False
        refreshed = snapshot.get("setup_refreshed_at")
        if refreshed:
            try:
                stale = (opened - datetime.fromisoformat(str(refreshed).replace("Z", "+00:00"))).total_seconds() > 900
            except (ValueError, TypeError):
                pass
        traps = []
        if result == "loss":
            if change is not None and trade["side"] == "LONG" and change >= 8:
                traps.extend(["late_entry_after_pump", "excessive_24h_move"])
            if change is not None and trade["side"] == "SHORT" and change <= -8:
                traps.append("excessive_24h_move")
            if atr is not None and atr >= 4:
                traps.append("high_atr_volatility")
            if atr and stop is not None and abs(entry - stop) / entry * 100 < atr * .5:
                traps.append("stop_too_tight")
            bullish = sum(any(x in label for x in ("bull", "buy", "up")) for label in labels)
            bearish = sum(any(x in label for x in ("bear", "sell", "down")) for label in labels)
            if bullish and bearish:
                traps.append("weak_multi_timeframe_confirmation")
            if resistance and trade["side"] == "LONG" and 0 <= (resistance-entry)/entry*100 <= 1:
                traps.append("nearby_resistance_for_long")
            if support and trade["side"] == "SHORT" and 0 <= (entry-support)/entry*100 <= 1:
                traps.append("nearby_support_for_short")
            if stale:
                traps.append("stale_setup")
        traps = list(dict.fromkeys(traps))
        primary_trap = traps[0] if traps else ("unknown" if result == "loss" else None)
        confidence = self._number(snapshot.get("confidence"))
        risk = self._number(snapshot.get("risk_score"))
        if confidence is not None and confidence <= 1: confidence *= 100
        if risk is not None and risk <= 1: risk *= 100
        quality = max(0, min(100, (confidence if confidence is not None else 50) - (risk or 0) * .25 - len(traps) * 8))
        grade = "A" if result == "win" and quality >= 70 else "B" if result == "win" else "C" if result == "breakeven" else "D" if quality >= 50 else "F"
        lesson = (f"Paper setup worked and closed by {trade['close_reason'].replace('_', ' ')}; keep validating it with more samples."
                  if result == "win" else
                  f"Possible trap: {primary_trap.replace('_', ' ')}; use this as a filter candidate, not a trade command."
                  if result == "loss" else "Paper trade finished near breakeven; review entry timing and costs.")
        max_price = self._number((position or {}).get("max_price")); min_price = self._number((position or {}).get("min_price"))
        favourable = adverse = None
        if max_price is not None and min_price is not None:
            favourable = ((max_price-entry) if trade["side"] == "LONG" else (entry-min_price)) / entry * 100
            adverse = ((entry-min_price) if trade["side"] == "LONG" else (max_price-entry)) / entry * 100
        return {"trade_id": trade["id"], "symbol": trade["symbol"], "market": trade["market"], "side": trade["side"],
                "opened_at": trade["opened_at"], "closed_at": trade["closed_at"],
                "holding_duration_seconds": max(0, (closed-opened).total_seconds()), "entry_price": entry,
                "exit_price": exit_price, "amount_invested": amount,
                "exit_value": self._number(trade.get("exit_value")) or amount+pnl, "realized_pnl": pnl,
                "realized_return_pct": return_pct, "close_reason": trade["close_reason"], "result": result,
                "did_follow_plan": "manual_review_required" if trade["close_reason"] == "manual" else "yes",
                "expected_direction_correct": result == "win" if result != "breakeven" else "unknown",
                "max_favourable_excursion_pct": favourable, "max_adverse_excursion_pct": adverse,
                "setup_quality_score": round(quality, 1), "post_trade_grade": grade,
                "trap_category": primary_trap, "trap_categories": traps, "one_line_lesson": lesson,
                "detailed_review_notes": f"Deterministic paper review based only on the immutable entry snapshot. {lesson}",
                "signal_snapshot": snapshot, "generated_at": _now()}

    @staticmethod
    def _save_review(db: sqlite3.Connection, review: dict) -> None:
        db.execute("INSERT OR REPLACE INTO paper_trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (review["trade_id"], review["symbol"], review["market"], review["side"], review["result"],
                    review["close_reason"], review["realized_return_pct"], review["post_trade_grade"],
                    review["trap_category"], review["one_line_lesson"], json.dumps(_safe_snapshot(review), default=str), review["generated_at"]))

    def reviews(self, trade_id: str | None = None, symbol: str | None = None) -> list[dict]:
        query, params = "SELECT review_json FROM paper_trade_reviews", []
        clauses = []
        if trade_id: clauses.append("trade_id=?"); params.append(trade_id)
        if symbol: clauses.append("symbol=?"); params.append(symbol.upper())
        if clauses: query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY generated_at DESC"
        with self._database() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            try:
                review = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(review, dict):
                result.append(review)
        return result

    def regenerate_review(self, trade_id: str) -> dict:
        with self._lock, self._database() as db:
            row = db.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if not row: raise KeyError(trade_id)
            trade = dict(row)
            position = db.execute("SELECT * FROM paper_positions WHERE id=?", (trade["position_id"],)).fetchone()
            review = self._build_review(trade, dict(position) if position else None)
            self._save_review(db, review)
        return review

    @staticmethod
    def _bucket(value: float | None, ranges: list[tuple[float, float, str]]) -> str:
        if value is None: return "unknown"
        return next((label for low, high, label in ranges if low <= value < high), ranges[-1][2])

    def learning(self, symbol: str | None = None) -> dict:
        reviews = self.reviews(symbol=symbol)
        for review in reviews:
            review["realized_return_pct"] = self._number(review.get("realized_return_pct")) or 0.0
            review["realized_pnl"] = self._number(review.get("realized_pnl")) or 0.0
        returns = [x["realized_return_pct"] for x in reviews]
        wins = [x for x in reviews if x.get("result") == "win"]
        losses = [x for x in reviews if x.get("result") == "loss"]
        def grouped(key):
            groups = {}
            for review in reviews:
                value = review.get(key)
                if value is not None: groups.setdefault(str(value), []).append(review["realized_return_pct"])
            return {name: {"trades": len(values), "average_return_pct": sum(values)/len(values),
                           "win_rate": sum(v > 0 for v in values)/len(values)*100} for name, values in groups.items()}
        def extremes(key):
            values = grouped(key)
            ordered = sorted(values, key=lambda name: values[name]["average_return_pct"], reverse=True)
            return ((ordered[0] if ordered else None), (ordered[-1] if ordered else None))
        bucketed = {"confidence": {}, "risk_score": {}, "atr": {}, "change_24h": {}, "timeframe_confirmation": {}}
        for review in reviews:
            snap = review.get("signal_snapshot") or {}
            if not isinstance(snap, dict): snap = {}
            confidence = self._number(snap.get("confidence")); risk = self._number(snap.get("risk_score"))
            if confidence is not None and confidence <= 1: confidence *= 100
            if risk is not None and risk <= 1: risk *= 100
            atr = self._number(snap.get("atr_pct")); change = self._number(snap.get("change_24h"))
            rows = snap.get("multi_timeframe_rows") or []
            if not isinstance(rows, list): rows = []
            labels = [str(x.get("signal") or x.get("trend") or x.get("bias") or "").lower() for x in rows if isinstance(x, dict)]
            mixed = any(any(k in x for k in ("bull","buy","up")) for x in labels) and any(any(k in x for k in ("bear","sell","down")) for x in labels)
            names = {
                "confidence": self._bucket(confidence, [(50,60,"50–60"),(60,70,"60–70"),(70,80,"70–80"),(80,90,"80–90"),(90,float("inf"),"90+")]),
                "risk_score": self._bucket(risk, [(0,26,"0–25"),(26,51,"26–50"),(51,76,"51–75"),(76,float("inf"),"76–100")]),
                "atr": self._bucket(atr, [(0,1,"under 1%"),(1,2,"1–2%"),(2,4,"2–4%"),(4,float("inf"),"4%+")]),
                "change_24h": self._bucket(abs(change) if change is not None else None, [(0,2,"under 2%"),(2,5,"2–5%"),(5,10,"5–10%"),(10,float("inf"),"10%+")]),
                "timeframe_confirmation": "mixed" if mixed else "aligned" if labels else "unknown"}
            for kind, name in names.items(): bucketed[kind].setdefault(name, []).append(review["realized_return_pct"])
        for kind, values in bucketed.items():
            bucketed[kind] = {name: {"trades": len(items), "average_return_pct": sum(items)/len(items),
                                     "win_rate": sum(x > 0 for x in items)/len(items)*100} for name, items in values.items()}
        best_market, worst_market = extremes("market"); best_symbol, worst_symbol = extremes("symbol")
        # A single market cannot honestly be both the best and worst performer.
        # Keep the useful label while making the missing comparison explicit.
        if best_market is not None and best_market == worst_market:
            worst_market = "insufficient market variety"
        best_side, worst_side = extremes("side")
        close_reasons = grouped("close_reason"); traps = grouped("trap_category")
        common = lambda values: max(values, key=lambda k: values[k]["trades"], default=None)
        gross_wins = sum(max(0, x["realized_pnl"]) for x in reviews)
        gross_losses = abs(sum(min(0, x["realized_pnl"]) for x in reviews))
        recommendation = {"recommendation": "needs_more_samples", "reason": "Learning insights are early; keep reviewing paper trades.", "confidence": "low", "sample_size": len(reviews)}
        if len(losses) >= 3:
            trap = common(traps)
            recommendation = {"recommendation": "tighten_filter", "reason": f"Loss reviews repeatedly identify {str(trap or 'uncertain setups').replace('_',' ')}.",
                              "confidence": "medium" if len(reviews) >= 20 else "low", "sample_size": len(reviews)}
        return {"total_closed_trades": len(self.trades()) if symbol is None else len(reviews), "reviewed_trades": len(reviews),
                "small_sample": len(reviews) < 5,
                "win_rate": len(wins)/len(reviews)*100 if reviews else 0, "average_return_pct": sum(returns)/len(returns) if returns else 0,
                "average_win_pct": sum(x["realized_return_pct"] for x in wins)/len(wins) if wins else 0,
                "average_loss_pct": sum(x["realized_return_pct"] for x in losses)/len(losses) if losses else 0,
                "profit_factor": gross_wins/gross_losses if gross_losses else (None if not gross_wins else "infinite"),
                "best_market": best_market, "worst_market": worst_market, "best_symbol": best_symbol, "worst_symbol": worst_symbol,
                "best_side": best_side, "worst_side": worst_side,
                "average_holding_time_seconds": sum(self._number(x.get("holding_duration_seconds")) or 0 for x in reviews)/len(reviews) if reviews else 0,
                "most_common_close_reason": common(close_reasons), "most_common_trap_category": common(traps),
                "confidence_bucket_performance": bucketed["confidence"], "risk_score_bucket_performance": bucketed["risk_score"],
                "atr_bucket_performance": bucketed["atr"], "change_24h_bucket_performance": bucketed["change_24h"],
                "timeframe_confirmation_performance": bucketed["timeframe_confirmation"],
                "small_sample_warning": "Learning insights are early. More paper trades are needed before making strong conclusions." if len(reviews) < 20 else None,
                "learning_recommendation": recommendation}

    def trades(self) -> list[dict]:
        with self._database() as db:
            rows = db.execute(
                "SELECT t.*, p.display_name FROM paper_trades t "
                "LEFT JOIN paper_positions p ON p.id=t.position_id ORDER BY t.closed_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["signal_snapshot"] = json.loads(item["signal_snapshot"])
            except (TypeError, json.JSONDecodeError):
                item["signal_snapshot"] = {}
            item["result"] = "win" if item["realized_pnl"] > 0 else "loss" if item["realized_pnl"] < 0 else "breakeven"
            # Older rows remain useful after the additive migration.
            item["entry_notional"] = item.get("entry_notional") or item["entry_price"] * item["quantity"]
            item["exit_value"] = item.get("exit_value") or item["exit_price"] * item["quantity"]
            item["amount_invested"] = item["entry_notional"]
            item["position_status"] = {"stop_loss": "Closed by stop loss",
                                       "take_profit": "Closed by take profit",
                                       "manual": "Closed manually"}.get(item["close_reason"], "Closed manually")
            result.append(item)
        return result

    def watchlist(self) -> list[dict]:
        with self._database() as db:
            return [dict(x) for x in db.execute("SELECT * FROM watchlist_items ORDER BY added_at DESC")]

    def add_watchlist(self, item: dict) -> dict:
        values = (item["market"], item["symbol"].upper(), item.get("display_name") or item["symbol"].upper(),
                  _now(), item.get("latest_action"), item.get("latest_confidence"), item.get("latest_price"))
        with self._lock, self._database() as db:
            # Do not replace the row: preserving added_at makes duplicate clicks idempotent.
            db.execute("INSERT INTO watchlist_items VALUES (?,?,?,?,?,?,?) "
                       "ON CONFLICT(market,symbol) DO UPDATE SET "
                       "display_name=excluded.display_name, latest_action=excluded.latest_action, "
                       "latest_confidence=excluded.latest_confidence, latest_price=excluded.latest_price", values)
        return next(x for x in self.watchlist() if x["market"] == values[0] and x["symbol"] == values[1])

    def delete_watchlist(self, market: str, symbol: str) -> bool:
        with self._lock, self._database() as db:
            cursor = db.execute("DELETE FROM watchlist_items WHERE market=? AND symbol=?", (market, symbol.upper()))
            return cursor.rowcount > 0

    def journal(self) -> list[dict]:
        with self._database() as db:
            return [dict(x) for x in db.execute("SELECT * FROM journal_notes ORDER BY created_at DESC")]

    def add_note(self, note: str, position_id: str | None = None, symbol: str | None = None) -> dict:
        if not isinstance(note, str) or not note.strip():
            raise ValueError("Note cannot be empty")
        if position_id:
            with self._database() as db:
                if not db.execute("SELECT 1 FROM paper_positions WHERE id=?", (position_id,)).fetchone():
                    raise ValueError("Paper position was not found")
        item = {"id": uuid.uuid4().hex, "position_id": position_id,
                "symbol": symbol.upper() if symbol else None, "note": note.strip()[:4000], "created_at": _now()}
        with self._lock, self._database() as db:
            db.execute("INSERT INTO journal_notes VALUES (?,?,?,?,?)", tuple(item.values()))
        return item
