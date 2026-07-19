import tempfile
import unittest
from pathlib import Path

from autotrade.journal import OrderJournal


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(Path(self.directory.name) / "state.db")

    def tearDown(self) -> None:
        self.journal.close()
        self.directory.cleanup()

    def test_terminal_order_state_cannot_regress(self) -> None:
        payload = {
            "symbol": "BTCUSDT",
            "clientOrderId": "at-entry-1",
            "orderId": 1,
            "side": "BUY",
            "type": "MARKET",
            "status": "NEW",
            "origQty": "0.001",
        }
        self.journal.record_order(payload, family="ORDINARY", role="ENTRY")
        self.assertTrue(
            self.journal.transition_order(
                "at-entry-1",
                "FILLED",
                event_type="TRADE",
                exchange_time=10,
                payload={"status": "FILLED"},
            )
        )
        self.assertFalse(
            self.journal.transition_order(
                "at-entry-1",
                "NEW",
                event_type="STALE",
                exchange_time=9,
                payload={"status": "NEW"},
            )
        )
        self.assertEqual(self.journal.order("at-entry-1")["status"], "FILLED")

    def test_algo_finished_can_be_completed_by_filled_execution(self) -> None:
        payload = {
            "symbol": "BTCUSDT",
            "clientAlgoId": "at-stop-finished",
            "algoId": 7,
            "side": "SELL",
            "orderType": "STOP_MARKET",
            "algoStatus": "NEW",
            "quantity": "0.001",
            "reduceOnly": True,
        }
        self.journal.record_order(payload, family="ALGO", role="STOP")
        self.assertTrue(
            self.journal.transition_order(
                "at-stop-finished",
                "FINISHED",
                event_type="ALGO_UPDATE",
                exchange_time=20,
                payload={"X": "FINISHED"},
            )
        )
        self.assertTrue(
            self.journal.transition_order(
                "at-stop-finished",
                "FILLED",
                event_type="TRADE",
                exchange_time=21,
                payload={"X": "FILLED"},
            )
        )
        self.assertEqual(self.journal.order("at-stop-finished")["status"], "FILLED")

    def test_duplicate_order_event_is_idempotent(self) -> None:
        payload = {
            "symbol": "BTCUSDT",
            "clientOrderId": "at-entry-2",
            "side": "BUY",
            "type": "MARKET",
            "status": "NEW",
        }
        self.journal.record_order(payload, family="ORDINARY", role="ENTRY")
        for _ in range(2):
            self.journal.transition_order(
                "at-entry-2", "NEW", event_type="NEW", exchange_time=11, payload=payload
            )
        new_events = [
            event for event in self.journal.order_events("at-entry-2")
            if event["event_type"] == "NEW"
        ]
        self.assertEqual(len(new_events), 1)

    def test_operator_command_lifecycle(self) -> None:
        command_id = self.journal.enqueue_command("CANCEL_ALL", {"symbol": "BTCUSDT"})
        self.assertEqual(self.journal.pending_commands()[0]["payload"]["symbol"], "BTCUSDT")
        self.assertTrue(self.journal.mark_command_running(command_id))
        self.journal.complete_command(command_id, result={"ok": True})
        self.assertEqual(self.journal.recent_commands(1)[0]["status"], "COMPLETED")

    def test_strategy_signal_is_enqueued_at_most_once(self) -> None:
        payload = {
            "intent_id": "intent-1",
            "source": "strategy:ema-atr-v1:1:signal-1",
            "symbol": "BTCUSDT",
        }
        first = self.journal.enqueue_strategy_signal("signal-1", payload)
        second = self.journal.enqueue_strategy_signal("signal-1", payload)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertTrue(self.journal.strategy_signal_command_exists("signal-1"))
        self.assertTrue(self.journal.pending_entry_command_exists())
        self.assertEqual(len(self.journal.pending_commands()), 1)

    def test_only_one_entry_intent_can_be_pending(self) -> None:
        first = self.journal.enqueue_strategy_signal(
            "signal-1",
            {"intent_id": "intent-1", "source": "strategy:test:1:signal-1"},
        )
        second = self.journal.enqueue_strategy_signal(
            "signal-2",
            {"intent_id": "intent-2", "source": "strategy:test:1:signal-2"},
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.journal.pending_commands()), 1)

    def test_candles_are_deduplicated(self) -> None:
        candle = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "open_time": 1,
            "close_time": 2,
            "open": "1",
            "high": "2",
            "low": "1",
            "close": "2",
            "volume": "3",
            "trade_count": 4,
            "closed": True,
        }
        self.assertTrue(self.journal.store_candle(candle))
        self.assertFalse(self.journal.store_candle(candle))

    def test_flat_reconciliation_closes_active_intents(self) -> None:
        self.journal.create_intent(
            client_order_id="at-entry-3",
            symbol="BTCUSDT",
            side="BUY",
            quantity="0.001",
            stop_price="60000",
            take_profit_price=None,
            details={},
        )
        self.assertEqual(self.journal.close_active_intents("BTCUSDT", "flat"), 1)
        self.assertIsNone(self.journal.latest_active_intent("BTCUSDT"))

    def test_control_health_recovery_can_be_recorded_as_info(self) -> None:
        self.journal.set_control(
            "user_stream_healthy", "true", "connected", severity="INFO"
        )
        event = self.journal.recent_audit(1)[0]
        self.assertEqual(event["event_type"], "user_stream_healthy")
        self.assertEqual(event["severity"], "INFO")


if __name__ == "__main__":
    unittest.main()
