"""Bounded public-data research jobs. No execution venue or credential access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import math
import os
import sqlite3
import threading
import time

from .models import MarketKind


def now():
    return datetime.now(timezone.utc).isoformat()


DEFAULTS = {
    "scanner_interval_seconds": 300, "monitor_interval_seconds": 60,
    # Legacy manual trades could reserve all cash. Preserve that ceiling at 1x.
    "max_position_allocation_pct": 100, "max_open_positions": 100,
    "max_total_exposure_pct": 100, "daily_loss_limit_pct": 100,
    "default_stop_loss_pct": 2, "default_take_profit_pct": 4,
    "futures_paper_leverage": 1,
}
BOUNDS = {
    "scanner_interval_seconds": (60, 86400), "monitor_interval_seconds": (5, 3600),
    "max_position_allocation_pct": (.1, 100), "max_open_positions": (1, 1000),
    "max_total_exposure_pct": (.1, 100), "daily_loss_limit_pct": (.1, 100),
    "default_stop_loss_pct": (.1, 50), "default_take_profit_pct": (.1, 100),
    "futures_paper_leverage": (1, 10),
}


class LocalState:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS research_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def read(self, key, default):
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT value FROM research_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def write(self, key, value):
        encoded = json.dumps(value, allow_nan=False)
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO research_state VALUES (?,?)", (key, encoded))

    def settings(self):
        return {**DEFAULTS, **self.read("settings", {})}

    def update_settings(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(BOUNDS):
            raise ValueError("Unknown paper-risk setting")
        for key, value in changes.items():
            low, high = BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            if key in {"max_open_positions", "scanner_interval_seconds", "monitor_interval_seconds"} and int(value) != value:
                raise ValueError(f"{key} must be an integer")
        with self.lock:
            result = {**self.settings(), **changes}
            self.write("settings", result)
        return result


class Job:
    def __init__(self, interval):
        self.interval = interval
        self.stop_event = threading.Event()
        self.gate = threading.Lock()
        self.state_lock = threading.RLock()
        self.thread = None
        self.manual_thread = None
        self.state = {"running": False, "state": "idle", "last_completed": None,
                      "last_successful_evaluation": None, "duration_seconds": None,
                      "next_scan": None, "failures": 0, "stale": False}

    def status(self):
        with self.state_lock:
            return dict(self.state)

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name=type(self).__name__)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            self.run()
            seconds = self.interval()
            with self.state_lock:
                self.state["next_scan"] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            self.stop_event.wait(seconds)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()  # Providers have finite transport timeouts; retain ownership until drained.
        if self.manual_thread:
            self.manual_thread.join()

    def request_run(self):
        if self.stop_event.is_set() or not self.gate.acquire(blocking=False):
            return False
        with self.state_lock:
            self.state.update(running=True, state="running")
        self.manual_thread = threading.Thread(target=self._execute, daemon=True, name="manual-scanner")
        self.manual_thread.start()
        return True

    def run(self):
        if self.stop_event.is_set() or not self.gate.acquire(blocking=False):
            return False
        return self._execute()

    def _execute(self):
        started = time.monotonic()
        with self.state_lock:
            self.state.update(running=True, state="running")
        try:
            self.evaluate()
        except Exception:
            with self.state_lock:
                self.state.update(stale=True, error="Public market data temporarily unavailable")
                self.state["failures"] += 1
        finally:
            with self.state_lock:
                self.state.update(running=False, state="idle", last_completed=now(),
                                  duration_seconds=round(time.monotonic() - started, 3))
            self.gate.release()
        return True


class Scanner(Job):
    def __init__(self, state, universe, analyze, candidates=12, concurrency=2):
        super().__init__(lambda: state.settings()["scanner_interval_seconds"])
        self.local, self.universe, self.analyze = state, universe, analyze
        self.candidates = max(1, min(50, candidates))
        self.concurrency = max(1, min(4, concurrency))
        self.rate_lock = threading.Lock()
        self.last_request = 0.0
        self.provider_failures = {}
        self.cooldowns = {}
        self.state.update(instruments_considered=0, instruments_analysed=0)

    def latest(self):
        data = self.local.read("scanner", {"results": [], "data_timestamp": None, "markets": {}, "attempted_at": None})
        stale = self.status()["stale"] or data.get("stale", False) or not data["data_timestamp"]
        if data["data_timestamp"]:
            stale |= (datetime.now(timezone.utc) - datetime.fromisoformat(data["data_timestamp"])).total_seconds() > self.interval() * 2
        return {**data, "stale": stale, "results": [{**r, "stale": stale or r.get("stale", False)} for r in data["results"]]}

    @staticmethod
    def priority(row):
        volume = row.get("volume") or 0
        movement = abs(row.get("change") or 0)
        volatility = abs(row.get("volatility_24h") or movement)
        # Turnover is a liquidity proxy; reported range or movement measures volatility.
        return math.log1p(max(0, volume)) * (1 + min(movement, 30) / 30 + min(volatility, 30) / 60)

    def setup(self, market, row):
        if self.stop_event.is_set():
            return None
        # Pace symbol starts as well as bounding concurrency. Provider caches handle repeat reads.
        with self.rate_lock:
            if time.monotonic() < self.cooldowns.get(market, 0):
                return None
            if self.stop_event.wait(max(0, .25 - (time.monotonic() - self.last_request))):
                return None
            self.last_request = time.monotonic()
        try:
            quick = self.analyze(market, row["symbol"])
            price = float(quick["live_price"])
            timestamp = datetime.fromisoformat(quick["last_updated"].replace("Z", "+00:00"))
            if not math.isfinite(price) or price <= 0 or timestamp.tzinfo is None:
                raise ValueError("invalid provider quote")
            if (datetime.now(timezone.utc) - timestamp).total_seconds() > quick.get("stale_after_seconds", 600):
                raise ValueError("stale provider quote")
        except Exception:
            with self.rate_lock:
                failures = self.provider_failures.get(market, 0) + 1
                self.provider_failures[market] = failures
                if failures >= 3:
                    # Stop queued requests after repeated outages/rate limits; no immediate retries.
                    self.cooldowns[market] = time.monotonic() + 60
            raise
        with self.rate_lock:
            self.provider_failures[market] = 0
        signal, plan = quick["signal"], quick["risk_plan"]
        action = str(getattr(signal["side"], "value", signal["side"])).removeprefix("Side.")
        action = {"STRONG_BUY": "BUY", "STRONG_SELL": "SELL"}.get(action, action)
        missing = [key for key in ("funding_rate", "order_book_imbalance", "open_interest", "open_interest_change") if row.get(key) is None]
        confidence = float(signal.get("confidence", 0)) * (1 - .025 * len(missing))
        risk = plan.get("risk_score", signal.get("risk_score", 1))
        score = round(confidence * (1 - risk) * 100, 2)
        return {"market": market.value, "symbol": row["symbol"], "action": action,
                "opportunity_score": score, "confidence": confidence,
                "probability": signal.get("probability"), "risk_score": risk,
                "entry_reference_price": quick["live_price"], "stop_loss": plan.get("stop_loss"),
                "take_profit": plan.get("take_profit"), "position_size_suggestion": min(plan.get("position_size_pct", .01), self.local.settings()["max_position_allocation_pct"] / 100),
                "multi_timeframe_agreement": quick.get("timeframe_breakdown", []),
                "momentum_summary": quick.get("momentum_summary"), "volatility_summary": quick.get("volatility_summary"),
                **{key: row.get(key) for key in ("funding_rate", "order_book_imbalance", "open_interest", "open_interest_change")},
                "unavailable_fields": missing, "why": quick.get("plain_language_reason", signal.get("rationale", "")) + (" Missing supplemental data reduces scanner confidence." if missing else ""),
                "data_timestamp": quick.get("last_updated"), "stale": False}

    def evaluate(self):
        previous = self.local.read("scanner", {"results": [], "markets": {}, "data_timestamp": None})
        results, markets, considered, analysed, failures = [], {}, 0, 0, 0
        for market in (MarketKind.CRYPTO_SPOT, MarketKind.CRYPTO_FUTURES):
            if self.stop_event.is_set():
                return
            try:
                universe = self.universe.universe(market)
                if universe["stale"]:
                    raise ValueError("stale universe")
                if not universe["instruments"]:
                    raise ValueError("empty WEEX universe")
                rows = [r for r in universe["instruments"] if r.get("price") is not None and r["price"] > 0 and (r.get("volume") or 0) > 0]
                considered += len(universe["instruments"])
                candidates = sorted(rows, key=self.priority, reverse=True)[:self.candidates]
                fresh = []
                market_failures = 0
                # Submit only the prefiltered shortlist. At most four symbols in flight.
                with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="scanner") as pool:
                    futures = [pool.submit(self.setup, market, row) for row in candidates]
                    for future in futures:
                        try:
                            result = future.result()
                            if result:
                                fresh.append(result)
                                analysed += 1
                        except Exception:
                            failures += 1
                            market_failures += 1
                if candidates and not fresh:
                    raise ValueError("no valid analyses")
                results.extend(fresh)
                if market_failures:
                    symbols = {r["symbol"] for r in fresh}
                    results.extend({**r, "stale": True} for r in previous["results"] if r["market"] == market.value and r["symbol"] not in symbols)
                markets[market.value] = {"stale": bool(market_failures), "status": "partial" if market_failures else "available", "data_timestamp": now()}
            except Exception:
                failures += 1
                results.extend({**r, "stale": True} for r in previous["results"] if r["market"] == market.value)
                markets[market.value] = {"stale": True, "status": "stale" if any(r["market"] == market.value for r in results) else "unavailable", "data_timestamp": previous.get("markets", {}).get(market.value, {}).get("data_timestamp")}
        results.sort(key=lambda r: (r["stale"], r["action"] not in {"BUY", "SELL"}, -r["opportunity_score"]))
        stale = bool(failures)
        timestamp = now() if not stale else previous.get("data_timestamp")
        self.local.write("scanner", {"results": results, "markets": markets, "data_timestamp": timestamp, "attempted_at": now(), "stale": stale})
        with self.state_lock:
            self.state.update(stale=stale, failures=self.state["failures"] + failures,
                              instruments_considered=considered, instruments_analysed=analysed,
                              last_successful_evaluation=timestamp, error="Some WEEX data unavailable" if stale else None)


class PaperMonitor(Job):
    def __init__(self, state, refresh):
        super().__init__(lambda: state.settings()["monitor_interval_seconds"])
        self.refresh = refresh

    def evaluate(self):
        positions = self.refresh()
        stale = any(not p.get("price_available", True) for p in positions)
        with self.state_lock:
            self.state.update(stale=stale, error="Some prices unavailable; stored marks retained" if stale else None)
            if stale:
                self.state["failures"] += 1
            else:
                self.state["last_successful_evaluation"] = now()
