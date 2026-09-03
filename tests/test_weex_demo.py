import base64, hashlib, hmac, os, unittest
from decimal import Decimal
from unittest.mock import patch
from tradebot.models import Side
from tradebot.weex import FuturesOrder, WeexCredentials, WeexDemoFuturesBroker, WeexV3Transport, live_execution_enabled

class MemoryLedger:
    def __init__(self): self.ids = set()
    def contains(self, key): return key in self.ids
    def record(self, key): self.ids.add(key)

class FakeTransport:
    def __init__(self, positions=None, success=True):
        self.current, self.success, self.calls = positions or [], success, []
    def request(self, method, path, params=None, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET": return self.current
        return {"success": self.success, "orderId": "123", "clientOrderId": payload["newClientOrderId"],
                "errorCode": "X" if not self.success else "", "errorMessage": "no" if not self.success else ""}

def an_order(exit_only=False):
    return FuturesOrder("BTCSUSDT", Side.BUY, "LONG", Decimal("1"), "strategy-001",
                        Decimal("60000"), Decimal("70000"), exit_only)

class WeexDemoTests(unittest.TestCase):
    def test_signature_matches_spec_formula(self):
        transport = WeexV3Transport(WeexCredentials("key", "secret", "pass"))
        body = '{"symbol":"BTCSUSDT"}'
        message = f"123POST/capi/v3/sim/order{body}".encode()
        expected = base64.b64encode(hmac.new(b"secret", message, hashlib.sha256).digest()).decode()
        self.assertEqual(transport.signature("123", "POST", "/capi/v3/sim/order", body=body), expected)
    def test_demo_order_has_protection_and_records_id(self):
        transport, ledger = FakeTransport(), MemoryLedger()
        report = WeexDemoFuturesBroker(transport, ledger).execute(an_order())
        self.assertEqual(report.mode, "weex_demo")
        self.assertEqual(transport.calls[-1][2]["SlWorkingType"], "MARK_PRICE")
        self.assertTrue(ledger.contains("strategy-001"))
    def test_duplicate_order_is_rejected(self):
        broker = WeexDemoFuturesBroker(FakeTransport(), MemoryLedger())
        broker.execute(an_order())
        with self.assertRaisesRegex(ValueError, "duplicate"): broker.execute(an_order())
    def test_exit_only_cannot_exceed_position(self):
        transport = FakeTransport([{"symbol": "BTCSUSDT", "side": "LONG", "size": "0.5"}])
        with self.assertRaisesRegex(ValueError, "exceeds"):
            WeexDemoFuturesBroker(transport, MemoryLedger()).execute(an_order(True))
    def test_cross_margin_and_high_leverage_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "isolated"):
            WeexDemoFuturesBroker(FakeTransport(), MemoryLedger(), required_margin_type="CROSSED")
        with self.assertRaisesRegex(ValueError, "1x to 5x"):
            WeexDemoFuturesBroker(FakeTransport(), MemoryLedger(), max_leverage=20)
    def test_live_requires_two_flags(self):
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=True):
            self.assertFalse(live_execution_enabled())

if __name__ == "__main__":
    unittest.main()
