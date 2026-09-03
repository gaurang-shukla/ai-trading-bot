import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import ExecutionReport, Side


@dataclass(frozen=True)
class WeexCredentials:
    api_key: str
    secret_key: str
    passphrase: str

    @classmethod
    def from_env(cls):
        values = (os.getenv("WEEX_API_KEY", ""), os.getenv("WEEX_SECRET_KEY", ""),
                  os.getenv("WEEX_PASSPHRASE", ""))
        if not all(values):
            raise ValueError("WEEX_API_KEY, WEEX_SECRET_KEY and WEEX_PASSPHRASE are required")
        return cls(*values)


class WeexV3Transport:
    base_url = "https://api-contract.weex.com"

    def __init__(self, credentials: WeexCredentials, clock_ms=None, opener=urlopen):
        self.credentials = credentials
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.opener = opener

    def signature(self, timestamp: str, method: str, path: str,
                  query: str = "", body: str = "") -> str:
        target = path + (f"?{query}" if query else "")
        message = f"{timestamp}{method.upper()}{target}{body}".encode()
        digest = hmac.new(self.credentials.secret_key.encode(), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def request(self, method: str, path: str, params: dict | None = None,
                payload: dict | None = None):
        query = urlencode(params or {})
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        timestamp = str(self.clock_ms())
        headers = {
            "ACCESS-KEY": self.credentials.api_key,
            "ACCESS-PASSPHRASE": self.credentials.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": self.signature(timestamp, method, path, query, body),
            "Content-Type": "application/json",
            "User-Agent": "three-layer-tradebot/0.2",
        }
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = Request(url, data=body.encode() if body else None,
                          method=method.upper(), headers=headers)
        with self.opener(request, timeout=15) as response:
            return json.load(response)


@dataclass(frozen=True)
class FuturesOrder:
    symbol: str
    side: Side
    position_side: str
    quantity: Decimal
    client_order_id: str
    stop_loss: Decimal
    take_profit: Decimal | None = None
    exit_only: bool = False


class IdempotencyLedger:
    def __init__(self, path: str = "state/weex-demo-orders.json"):
        self.path = Path(path)
        self._ids = set(json.loads(self.path.read_text())) if self.path.exists() else set()

    def contains(self, key: str) -> bool:
        return key in self._ids

    def record(self, key: str) -> None:
        self._ids.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(sorted(self._ids)))
        temp.replace(self.path)


class WeexDemoFuturesBroker:
    """WEEX V3 simulation broker. It cannot address real-order paths."""

    mode = "weex_demo"
    order_path = "/capi/v3/sim/order"
    positions_path = "/capi/v3/sim/position/allPosition"

    def __init__(self, transport: WeexV3Transport, ledger: IdempotencyLedger | None = None,
                 max_leverage: int = 3, required_margin_type: str = "ISOLATED"):
        if not 1 <= max_leverage <= 5:
            raise ValueError("demo safety policy permits leverage from 1x to 5x")
        if required_margin_type != "ISOLATED":
            raise ValueError("only isolated margin is permitted")
        self.transport, self.ledger = transport, ledger or IdempotencyLedger()
        self.max_leverage, self.required_margin_type = max_leverage, required_margin_type

    def positions(self) -> list[dict]:
        return self.transport.request("GET", self.positions_path)

    def _validate(self, order: FuturesOrder, positions: list[dict]) -> None:
        if not re.fullmatch(r"[\.A-Z\:/a-z0-9_-]{1,36}", order.client_order_id):
            raise ValueError("invalid WEEX client order id")
        if order.quantity <= 0 or order.stop_loss <= 0:
            raise ValueError("positive quantity and stop loss are mandatory")
        if order.position_side not in {"LONG", "SHORT"}:
            raise ValueError("position_side must be LONG or SHORT")
        if self.ledger.contains(order.client_order_id):
            raise ValueError("duplicate client order id")
        if order.exit_only:
            matching = [p for p in positions if p.get("symbol") == order.symbol
                        and p.get("side") == order.position_side]
            available = sum(Decimal(str(p.get("size", "0"))) for p in matching)
            if order.quantity > available:
                raise ValueError("exit-only quantity exceeds reconciled position")

    def execute(self, order: FuturesOrder) -> ExecutionReport:
        current = self.positions()
        self._validate(order, current)
        payload = {
            "symbol": order.symbol,
            "side": order.side.value,
            "positionSide": order.position_side,
            "type": "MARKET",
            "quantity": format(order.quantity, "f"),
            "newClientOrderId": order.client_order_id,
            "slTriggerPrice": format(order.stop_loss, "f"),
            "SlWorkingType": "MARK_PRICE",
        }
        if order.take_profit is not None:
            payload.update(tpTriggerPrice=format(order.take_profit, "f"),
                           TpWorkingType="MARK_PRICE")
        result = self.transport.request("POST", self.order_path, payload=payload)
        if not result.get("success"):
            raise RuntimeError(f"WEEX rejected order: {result.get('errorCode')} {result.get('errorMessage')}")
        self.ledger.record(order.client_order_id)
        return ExecutionReport(str(result["orderId"]), "ACCEPTED", order.symbol, order.side,
                               int(order.quantity), 0.0, self.mode)


def live_execution_enabled() -> bool:
    # Both flags are required by design; no live broker exists in this release.
    return os.getenv("TRADING_MODE") == "live" and os.getenv("WEEX_LIVE_ENABLED", "").lower() == "true"
