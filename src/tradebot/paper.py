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
            db.execute("INSERT OR IGNORE INTO paper_account VALUES (1,?,?,0)",
                       (self.starting_cash, self.starting_cash))
            db.execute("PRAGMA user_version=1")

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
            db.execute("UPDATE paper_positions SET current_price=?,price_available=1 WHERE id=? AND status='open'", (price, position_id))

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
                      price: float, notional: float, signal: dict, risk_plan: dict) -> dict:
        if side not in {"LONG", "SHORT"}:
            raise ValueError("Side must be LONG or SHORT")
        price = _positive_number(price, "Live price")
        notional = _positive_number(notional, "Notional amount")
        quantity = notional / price
        if not math.isfinite(quantity) or not (0 < quantity <= 1e18):
            raise ValueError("Calculated quantity is invalid")
        item = {"id": uuid.uuid4().hex, "market": market, "symbol": symbol.upper(),
                "display_name": display_name, "side": side, "entry_price": price,
                "current_price": price, "quantity": quantity, "notional_value": notional,
                "stop_loss": risk_plan.get("stop_loss"), "take_profit": risk_plan.get("take_profit"),
                "risk_score": risk_plan.get("risk_score"), "confidence": signal.get("confidence"),
                "position_size_pct": risk_plan.get("position_size_pct"), "opened_at": _now(),
                "source_signal_action": str(signal.get("side", "HOLD")), "status": "open",
                "signal_snapshot": _safe_snapshot({"signal": signal, "risk_plan": risk_plan,
                                                   "live_price": price})}
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
            values = list(item.values()); values[-1] = json.dumps(values[-1], default=str)
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
        trade["signal_snapshot"] = json.loads(trade["signal_snapshot"])
        trade["result"] = "win" if profit > 0 else "loss" if profit < 0 else "breakeven"
        return trade

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
