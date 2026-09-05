"""Durable, local-only paper portfolio accounting.

This module deliberately has no dependency on an execution venue.  Prices are
passed in by the web service and every mutation is recorded in local SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperStore:
    """Small SQLite repository for the paper account, positions and journal."""

    def __init__(self, path: str | Path | None = None, starting_cash: float | None = None):
        self.path = Path(path or os.getenv("SIGNAL_DB_PATH", "data/signal.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.starting_cash = float(starting_cash or os.getenv("PAPER_STARTING_CASH", "100000"))
        if not 0 < self.starting_cash <= 1_000_000_000:
            raise ValueError("PAPER_STARTING_CASH must be a positive, sensible amount")
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

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
        with self._lock, self._connect() as db:
            db.executescript(schema)
            db.execute("INSERT OR IGNORE INTO paper_account VALUES (1,?,?,0)",
                       (self.starting_cash, self.starting_cash))

    @staticmethod
    def pnl(side: str, entry: float, current: float, quantity: float) -> float:
        return (current - entry) * quantity if side == "LONG" else (entry - current) * quantity

    def positions(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
        return [self._position(row) for row in rows]

    def _position(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["signal_snapshot"] = json.loads(item["signal_snapshot"])
        profit = self.pnl(item["side"], item["entry_price"], item["current_price"], item["quantity"])
        item["unrealized_pnl"] = profit
        item["unrealized_pnl_pct"] = profit / item["notional_value"] * 100
        return item

    def mark(self, position_id: str, price: float) -> None:
        if price <= 0:
            return
        with self._lock, self._connect() as db:
            db.execute("UPDATE paper_positions SET current_price=? WHERE id=? AND status='open'", (price, position_id))

    def account(self) -> dict[str, Any]:
        positions = self.positions()
        unrealized = sum(item["unrealized_pnl"] for item in positions)
        with self._connect() as db:
            account = dict(db.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            count, wins = db.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl>0),0) FROM paper_trades").fetchone()
        equity = account["cash_balance"] + sum(x["notional_value"] for x in positions) + unrealized
        return {"starting_balance": account["starting_balance"], "cash_balance": account["cash_balance"],
                "equity": equity, "realized_pnl": account["realized_pnl"], "unrealized_pnl": unrealized,
                "total_pnl": account["realized_pnl"] + unrealized,
                "win_rate": (wins / count * 100 if count else 0), "open_positions_count": len(positions),
                "closed_trades_count": count, "mode": "paper"}

    def open_position(self, *, market: str, symbol: str, display_name: str, side: str,
                      price: float, notional: float, signal: dict, risk_plan: dict) -> dict:
        if side not in {"LONG", "SHORT"} or price <= 0 or notional <= 0:
            raise ValueError("A valid side, live price, and positive notional amount are required")
        if notional > self.account()["cash_balance"]:
            raise ValueError("Notional amount exceeds available paper cash")
        quantity = notional / price
        if not (0 < quantity < 1e15):
            raise ValueError("Calculated quantity is invalid")
        item = {"id": uuid.uuid4().hex, "market": market, "symbol": symbol.upper(),
                "display_name": display_name, "side": side, "entry_price": price,
                "current_price": price, "quantity": quantity, "notional_value": notional,
                "stop_loss": risk_plan.get("stop_loss"), "take_profit": risk_plan.get("take_profit"),
                "risk_score": risk_plan.get("risk_score"), "confidence": signal.get("confidence"),
                "position_size_pct": risk_plan.get("position_size_pct"), "opened_at": _now(),
                "source_signal_action": str(signal.get("side", "HOLD")), "status": "open",
                "signal_snapshot": {"signal": signal, "risk_plan": risk_plan, "live_price": price}}
        with self._lock, self._connect() as db:
            db.execute("UPDATE paper_account SET cash_balance=cash_balance-? WHERE id=1 AND cash_balance>=?",
                       (notional, notional))
            if not db.execute("SELECT changes()").fetchone()[0]:
                raise ValueError("Not enough paper cash")
            columns = ",".join(item)
            values = list(item.values()); values[-1] = json.dumps(values[-1], default=str)
            db.execute(f"INSERT INTO paper_positions ({columns}) VALUES ({','.join('?' for _ in item)})", values)
        position_id = item["id"]
        return next(position for position in self.positions() if position["id"] == position_id)

    def close_position(self, position_id: str, price: float, reason: str = "Closed by user") -> dict:
        if price <= 0:
            raise ValueError("A valid live price is required to close")
        with self._lock, self._connect() as db:
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
                     "close_reason": reason[:200] or "Closed by user", "signal_snapshot": position["signal_snapshot"]}
            db.execute("UPDATE paper_positions SET status='closed',current_price=? WHERE id=?", (price, position_id))
            db.execute("UPDATE paper_account SET cash_balance=cash_balance+?, realized_pnl=realized_pnl+? WHERE id=1",
                       (position["notional_value"] + profit, profit))
            db.execute(f"INSERT INTO paper_trades ({','.join(trade)}) VALUES ({','.join('?' for _ in trade)})", list(trade.values()))
        trade["signal_snapshot"] = json.loads(trade["signal_snapshot"])
        trade["result"] = "win" if profit > 0 else "loss" if profit < 0 else "breakeven"
        return trade

    def trades(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM paper_trades ORDER BY closed_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row); item["signal_snapshot"] = json.loads(item["signal_snapshot"])
            item["result"] = "win" if item["realized_pnl"] > 0 else "loss" if item["realized_pnl"] < 0 else "breakeven"
            result.append(item)
        return result

    def watchlist(self) -> list[dict]:
        with self._connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM watchlist_items ORDER BY added_at DESC")]

    def add_watchlist(self, item: dict) -> dict:
        values = (item["market"], item["symbol"].upper(), item.get("display_name") or item["symbol"].upper(),
                  _now(), item.get("latest_action"), item.get("latest_confidence"), item.get("latest_price"))
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO watchlist_items VALUES (?,?,?,?,?,?,?)", values)
        return next(x for x in self.watchlist() if x["market"] == values[0] and x["symbol"] == values[1])

    def delete_watchlist(self, market: str, symbol: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM watchlist_items WHERE market=? AND symbol=?", (market, symbol.upper()))
            return cursor.rowcount > 0

    def journal(self) -> list[dict]:
        with self._connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM journal_notes ORDER BY created_at DESC")]

    def add_note(self, note: str, position_id: str | None = None, symbol: str | None = None) -> dict:
        if not note.strip():
            raise ValueError("Note cannot be empty")
        item = {"id": uuid.uuid4().hex, "position_id": position_id,
                "symbol": symbol.upper() if symbol else None, "note": note.strip()[:4000], "created_at": _now()}
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO journal_notes VALUES (?,?,?,?,?)", tuple(item.values()))
        return item
