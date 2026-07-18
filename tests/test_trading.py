import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autotrade.errors import ProtectionError, RuleViolation
from autotrade.journal import OrderJournal
from autotrade.trading import MAX_CLIENT_ID_LENGTH, TradingService, client_id
from tests.test_rules import EXCHANGE_INFO


class FakeSettings:
    environment = "testnet"


class FakeClient:
    settings = FakeSettings()

    def __init__(self, *, position: bool = False, stop_fails: bool = False) -> None:
        self.position = position
        self.stop_fails = stop_fails
        self.orders: list[dict] = []

    def sync_time(self):
        return 0

    def position_mode(self):
        return {"dualSidePosition": False}

    def positions(self, symbol=None):
        return [{"positionAmt": "0.001" if self.position else "0"}]

    def open_orders(self, symbol=None):
        return []

    def open_algo_orders(self, symbol=None):
        return []

    def change_leverage(self, symbol, leverage):
        return {"maxNotionalValue": "1000000"}

    def change_margin_type(self, symbol, margin_type):
        return {"code": 200, "marginType": margin_type}

    def account(self):
        return {"availableBalance": "1000"}

    def exchange_info(self, **kwargs):
        return EXCHANGE_INFO

    def mark_price(self, symbol):
        return {"markPrice": "50000"}

    def new_order(self, **params):
        self.orders.append(params)
        if params.get("newClientOrderId", "").startswith("at-emergency"):
            return {"status": "FILLED", "executedQty": params["quantity"]}
        return {"status": "FILLED", "executedQty": params["quantity"], "orderId": 42}

    def new_algo_order(self, **params):
        if self.stop_fails:
            raise RuntimeError("simulated stop rejection")
        return {"algoId": 7, **params}


class TradingServiceTests(unittest.TestCase):
    def make_journal(self, directory: str) -> OrderJournal:
        return OrderJournal(Path(directory) / "orders.db")

    def test_refuses_existing_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self.make_journal(directory)
            try:
                service = TradingService(FakeClient(position=True), journal)
                with self.assertRaises(RuleViolation):
                    service.place_market_bracket(
                        symbol="BTCUSDT",
                        side="BUY",
                        risk_usdt=Decimal("10"),
                        stop_price=Decimal("49000"),
                        take_profit_price=None,
                        leverage=3,
                    )
            finally:
                journal.close()

    def test_stop_failure_submits_reduce_only_emergency_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self.make_journal(directory)
            client = FakeClient(stop_fails=True)
            try:
                service = TradingService(client, journal)
                with self.assertRaises(ProtectionError):
                    service.place_market_bracket(
                        symbol="BTCUSDT",
                        side="BUY",
                        risk_usdt=Decimal("10"),
                        stop_price=Decimal("49000"),
                        take_profit_price=None,
                        leverage=3,
                    )
                self.assertEqual(len(client.orders), 2)
                emergency = client.orders[-1]
                self.assertEqual(emergency["side"], "SELL")
                self.assertTrue(emergency["reduceOnly"])
                self.assertEqual(journal.recent(1)[0]["status"], "EMERGENCY_CLOSED")
            finally:
                journal.close()

    def test_all_client_id_kinds_fit_binance_limit(self) -> None:
        for kind in ("entry", "stop", "take-profit", "emergency", "a-very-long-kind-name"):
            identifier = client_id(kind)
            self.assertLessEqual(len(identifier), MAX_CLIENT_ID_LENGTH)
            self.assertRegex(identifier, r"^[.A-Z:/a-z0-9_-]+$")

    def test_can_prepare_take_profit_for_existing_long(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = self.make_journal(directory)
            client = FakeClient(position=True)
            try:
                parameters = TradingService(client, journal).take_profit_parameters(
                    "BTCUSDT", Decimal("65000")
                )
                self.assertEqual(parameters["side"], "SELL")
                self.assertEqual(parameters["quantity"], "0.001")
                self.assertEqual(parameters["type"], "TAKE_PROFIT_MARKET")
                self.assertLessEqual(len(parameters["clientAlgoId"]), MAX_CLIENT_ID_LENGTH)
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
