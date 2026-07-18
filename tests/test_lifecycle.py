import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autotrade.journal import OrderJournal
from autotrade.trading import TradingService
from tests.test_rules import EXCHANGE_INFO


class LifecycleSettings:
    environment = "testnet"


class LifecycleClient:
    settings = LifecycleSettings()

    def __init__(self):
        self.amount = Decimal("0.002")
        self.calls = []
        self.algo_orders = [
            {
                "symbol": "BTCUSDT",
                "algoId": 10,
                "clientAlgoId": "old-stop",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "quantity": "0.002",
                "triggerPrice": "49000",
                "createTime": 1,
                "reduceOnly": True,
            }
        ]

    def sync_time(self, **kwargs):
        return 0

    def position_mode(self, **kwargs):
        return {"dualSidePosition": False}

    def positions(self, symbol=None, **kwargs):
        if self.amount == 0:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        return [{"symbol": "BTCUSDT", "positionAmt": str(self.amount)}]

    def exchange_info(self, **kwargs):
        return EXCHANGE_INFO

    def mark_price(self, symbol, **kwargs):
        return {"markPrice": "50000"}

    def new_order(self, **params):
        self.calls.append(("new_order", params))
        self.amount -= Decimal(params["quantity"])
        return {
            "symbol": params["symbol"],
            "clientOrderId": params["newClientOrderId"],
            "orderId": 20,
            "side": params["side"],
            "type": "MARKET",
            "status": "FILLED",
            "origQty": params["quantity"],
            "executedQty": params["quantity"],
            "reduceOnly": True,
        }

    def open_orders(self, symbol=None):
        return []

    def open_algo_orders(self, symbol=None, **kwargs):
        return list(self.algo_orders)

    def new_algo_order(self, **params):
        self.calls.append(("new_algo", params))
        result = {
            **params,
            "algoId": 11,
            "orderType": params["type"],
            "algoStatus": "NEW",
            "quantity": params["quantity"],
            "triggerPrice": params["triggerPrice"],
            "createTime": 2,
        }
        self.algo_orders.append(result)
        return result

    def cancel_algo_order(self, algo_id=None, client_algo_id=None):
        self.calls.append(("cancel_algo", {"algo_id": algo_id, "client": client_algo_id}))
        self.algo_orders = [
            order for order in self.algo_orders if order.get("clientAlgoId") != client_algo_id
        ]
        return {"algoId": algo_id, "clientAlgoId": client_algo_id, "algoStatus": "CANCELED"}

    def cancel_all_orders(self, symbol):
        self.calls.append(("cancel_all", symbol))
        return {"code": 200}

    def cancel_all_algo_orders(self, symbol):
        self.calls.append(("cancel_all_algo", symbol))
        self.algo_orders = []
        return {"code": 200}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(Path(self.directory.name) / "state.db")
        self.client = LifecycleClient()
        self.service = TradingService(self.client, self.journal)

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def test_full_close_uses_reduce_only_and_cleans_both_order_families(self):
        result = self.service.close_position("BTCUSDT")
        close_call = self.client.calls[0]
        self.assertEqual(close_call[0], "new_order")
        self.assertTrue(close_call[1]["reduceOnly"])
        self.assertEqual(self.client.amount, 0)
        self.assertIn(("cancel_all", "BTCUSDT"), self.client.calls)
        self.assertIn(("cancel_all_algo", "BTCUSDT"), self.client.calls)
        self.assertEqual(result["close"]["status"], "FILLED")

    def test_replacement_is_created_before_old_protection_is_canceled(self):
        result = self.service.replace_protection(
            "BTCUSDT", "STOP_MARKET", Decimal("49500")
        )
        operation_names = [call[0] for call in self.client.calls]
        self.assertLess(operation_names.index("new_algo"), operation_names.index("cancel_algo"))
        self.assertEqual(result["replacement"]["triggerPrice"], "49500")


if __name__ == "__main__":
    unittest.main()
