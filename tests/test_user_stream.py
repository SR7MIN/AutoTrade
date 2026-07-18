import unittest

from autotrade.user_stream import FlatPositionReconciler, RECONCILE_EVENTS


class ReconcileClient:
    def __init__(self, position_amount="0", ordinary=1, algo=1):
        self.position_amount = position_amount
        self.ordinary = ordinary
        self.algo = algo
        self.cancelled = []

    def positions(self, symbol):
        return [{"positionAmt": self.position_amount}]

    def open_orders(self, symbol):
        return [{}] * self.ordinary

    def open_algo_orders(self, symbol):
        return [{}] * self.algo

    def cancel_all_orders(self, symbol):
        self.cancelled.append(("ordinary", symbol))
        return {"code": 200}

    def cancel_all_algo_orders(self, symbol):
        self.cancelled.append(("algo", symbol))
        return {"code": 200}


class FlatPositionReconcilerTests(unittest.TestCase):
    def test_algo_updates_trigger_reconciliation(self):
        self.assertIn("ALGO_UPDATE", RECONCILE_EVENTS)

    def test_cancels_both_order_families_when_flat(self):
        client = ReconcileClient()
        reconciler = FlatPositionReconciler(client, "btcusdt")
        result = reconciler.handle({"e": "ACCOUNT_UPDATE"})
        self.assertEqual(result["ordinaryFound"], 1)
        self.assertEqual(result["algoFound"], 1)
        self.assertEqual(client.cancelled, [("ordinary", "BTCUSDT"), ("algo", "BTCUSDT")])

    def test_does_not_cancel_while_position_is_open(self):
        client = ReconcileClient(position_amount="0.01")
        reconciler = FlatPositionReconciler(client, "BTCUSDT")
        self.assertIsNone(reconciler.handle({"e": "ORDER_TRADE_UPDATE"}))
        self.assertEqual(client.cancelled, [])


if __name__ == "__main__":
    unittest.main()
