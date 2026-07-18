import asyncio
import tempfile
import unittest
from pathlib import Path

from autotrade.daemon import AccountReconciler, run_user_stream_loop
from autotrade.journal import OrderJournal


class ReconcileClient:
    def __init__(self, position_amount="0", ordinary=None, algo=None):
        self.position_amount = position_amount
        self.ordinary = ordinary or []
        self.algo = algo or []

    def sync_time(self):
        return 0

    def account(self):
        return {
            "totalWalletBalance": "1000", "availableBalance": "1000",
            "totalUnrealizedProfit": "0", "totalMaintMargin": "0",
        }

    def positions(self, symbol=None):
        return [
            {
                "symbol": "BTCUSDT", "positionAmt": self.position_amount,
                "liquidationPrice": "30000",
            }
        ]

    def open_orders(self, symbol=None):
        return self.ordinary

    def open_algo_orders(self, symbol=None):
        return self.algo

    def account_trades(self, symbol, start_time=None):
        return []

    def mark_price(self, symbol):
        return {"markPrice": "50000"}


class ReconcileService:
    def __init__(self):
        self.canceled = []
        self.closed = []

    def cancel_all(self, symbol):
        self.canceled.append(symbol)
        return {"symbol": symbol}

    def close_position(self, symbol):
        self.closed.append(symbol)
        return {"symbol": symbol}

    def sync_protection_quantities(self, symbol):
        return {"symbol": symbol}

    def protect_position(self, *args, **kwargs):
        return {"protected": True}


class RiskStub:
    def __init__(self):
        self.locked = []

    def lock_entries(self, reason):
        self.locked.append(reason)

    def evaluate_open_positions(self, positions, prices):
        return []


class AlertsStub:
    def __init__(self):
        self.events = []

    def emit(self, event_type, message, **kwargs):
        self.events.append(event_type)


class UserLoopReconciler:
    def __init__(self):
        self.startup_count = 0

    def startup_reconcile(self):
        self.startup_count += 1
        return {"BTCUSDT": {"position": "FLAT"}}


class ReconnectingStreamFixture:
    """First session fails after an event; second session stays connected."""

    def __init__(self):
        self.calls = 0
        self.first_event = asyncio.Event()
        self.second_connected = asyncio.Event()
        self.hold_second_session = asyncio.Event()

    async def __call__(self, client, ws_url, callback, *, on_connected=None):
        self.calls += 1
        if on_connected:
            on_connected()
        if self.calls == 1:
            await callback({"e": "ACCOUNT_UPDATE", "T": 1})
            self.first_event.set()
            raise RuntimeError("simulated user stream disconnect")
        self.second_connected.set()
        await self.hold_second_session.wait()


class DaemonReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(Path(self.directory.name) / "state.db")

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def test_flat_position_cleans_stale_orders(self):
        client = ReconcileClient(
            algo=[
                {
                    "symbol": "BTCUSDT", "clientAlgoId": "stale-stop", "algoId": 1,
                    "side": "SELL", "orderType": "STOP_MARKET", "algoStatus": "NEW",
                    "quantity": "0.001", "reduceOnly": True,
                }
            ]
        )
        service = ReconcileService()
        reconciler = AccountReconciler(
            client, self.journal, service, RiskStub(), AlertsStub(), ["BTCUSDT"], "pause"
        )
        result = reconciler.reconcile_symbol("BTCUSDT")
        self.assertEqual(service.canceled, ["BTCUSDT"])
        self.assertEqual(result["position"], "FLAT")

    def test_unprotected_position_locks_new_entries(self):
        client = ReconcileClient(position_amount="0.001")
        risk = RiskStub()
        alerts = AlertsStub()
        reconciler = AccountReconciler(
            client, self.journal, ReconcileService(), risk, alerts, ["BTCUSDT"], "pause"
        )
        result = reconciler.reconcile_symbol("BTCUSDT")
        self.assertEqual(result["status"], "UNPROTECTED")
        self.assertTrue(risk.locked)
        self.assertIn("UNPROTECTED_POSITION", alerts.events)

    def test_algo_update_transitions_algo_order_state(self):
        client = ReconcileClient(
            position_amount="0.001",
            algo=[
                {
                    "symbol": "BTCUSDT",
                    "clientAlgoId": "at-stop-event",
                    "algoId": 99,
                    "side": "SELL",
                    "orderType": "STOP_MARKET",
                    "algoStatus": "NEW",
                    "quantity": "0.001",
                    "triggerPrice": "49000",
                    "reduceOnly": True,
                }
            ],
        )
        service = ReconcileService()
        reconciler = AccountReconciler(
            client, self.journal, service, RiskStub(), AlertsStub(), ["BTCUSDT"], "pause"
        )
        reconciler.handle_event(
            {
                "e": "ALGO_UPDATE",
                "T": 100,
                "o": {
                    "caid": "at-stop-event",
                    "aid": 99,
                    "o": "STOP_MARKET",
                    "X": "NEW",
                    "S": "SELL",
                    "R": True,
                    "s": "BTCUSDT",
                    "tp": "49000",
                    "q": "0.001",
                },
            }
        )
        reconciler.handle_event(
            {
                "e": "ALGO_UPDATE",
                "T": 101,
                "o": {
                    "caid": "at-stop-event",
                    "aid": 99,
                    "o": "STOP_MARKET",
                    "X": "CANCELED",
                    "S": "SELL",
                    "R": True,
                    "s": "BTCUSDT",
                    "tp": "49000",
                    "q": "0.001",
                },
            }
        )
        order = self.journal.order("at-stop-event")
        self.assertEqual(order["family"], "ALGO")
        self.assertEqual(order["role"], "STOP")
        self.assertEqual(order["reduce_only"], 1)
        self.assertEqual(order["status"], "CANCELED")
        self.assertEqual(
            [event["event_type"] for event in self.journal.order_events("at-stop-event")].count(
                "ALGO_UPDATE"
            ),
            2,
        )

    def test_trade_lite_is_idempotent_with_order_trade_update(self):
        client = ReconcileClient()
        reconciler = AccountReconciler(
            client, self.journal, ReconcileService(), RiskStub(), AlertsStub(), ["BTCUSDT"], "pause"
        )
        trade_lite = {
            "e": "TRADE_LITE",
            "E": 200,
            "s": "BTCUSDT",
            "t": 1234,
            "i": 55,
            "c": "at-entry-fill",
            "S": "BUY",
            "l": "0.001",
            "L": "50000",
            "T": 200,
        }
        reconciler.handle_event(trade_lite)
        reconciler.handle_event(trade_lite)
        reconciler.handle_event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {
                    "s": "BTCUSDT",
                    "t": 1234,
                    "i": 55,
                    "c": "at-entry-fill",
                    "S": "BUY",
                    "q": "0.001",
                    "z": "0.001",
                    "ap": "50000",
                    "R": False,
                    "X": "FILLED",
                    "x": "TRADE",
                    "T": 200,
                    "o": "MARKET",
                    "n": "0.12",
                    "N": "USDT",
                    "rp": "0.34",
                },
            }
        )
        with self.journal._lock:
            row = self.journal._connection.execute(
                "SELECT COUNT(*) AS count, commission, realized_pnl FROM fills "
                "WHERE symbol='BTCUSDT' AND trade_id='1234'"
            ).fetchone()
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["commission"], "0.12")
        self.assertEqual(row["realized_pnl"], "0.34")

    def test_user_stream_fixture_reconciles_before_reconnect(self):
        fixture = ReconnectingStreamFixture()
        reconciler = UserLoopReconciler()
        risk = RiskStub()
        alerts = AlertsStub()
        received = []

        async def handle_event(event):
            received.append(event)

        async def no_sleep(_delay):
            return None

        async def scenario():
            task = asyncio.create_task(
                run_user_stream_loop(
                    object(),
                    "wss://test.invalid",
                    reconciler,
                    self.journal,
                    risk,
                    alerts,
                    asyncio.Lock(),
                    asyncio.Event(),
                    handle_event,
                    stream_runner=fixture,
                    sleep=no_sleep,
                    random_value=lambda: 0.0,
                )
            )
            await fixture.first_event.wait()
            await fixture.second_connected.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(fixture.calls, 2)
        self.assertEqual(reconciler.startup_count, 2)
        self.assertEqual(len(received), 1)
        self.assertTrue(risk.locked)
        self.assertIn("USER_STREAM_RECONNECT", alerts.events)
        self.assertEqual(self.journal.get_control("user_stream_healthy"), "true")


if __name__ == "__main__":
    unittest.main()
