import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from autotrade.daemon import TradingDaemon
from autotrade.intents import EntryIntent
from autotrade.journal import OrderJournal
from autotrade.strategy import (
    DivergenceEvidence,
    StrategyDecision,
    StrategyExitDecision,
    StrategySignal,
)


def decision() -> StrategyDecision:
    signal = StrategySignal(
        strategy="multi-divergence-reversal-v1",
        version="1",
        symbol="BTCUSDT",
        interval="5m",
        candle_open_time=700_001,
        candle_close_time=1_000_000,
        side="SELL",
        reference_price=Decimal("100"),
        stop_price=Decimal("110"),
        take_profit_price=Decimal("70"),
        risk_usdt=Decimal("1"),
        leverage=3,
        margin_utilization=Decimal("0.5"),
        indicators=(),
        reason="fixture",
        instance_id="divergence-test",
    )
    evidence = DivergenceEvidence(
        indicator="rsi",
        divergence_type="REGULAR",
        direction="BEARISH",
        current_pivot_time=600_000,
        previous_pivot_time=0,
        current_price=Decimal("105"),
        previous_price=Decimal("104"),
        current_indicator=Decimal("60"),
        previous_indicator=Decimal("70"),
    )
    return StrategyDecision(
        strategy=signal.strategy,
        version=signal.version,
        instance_id="divergence-test",
        symbol=signal.symbol,
        interval=signal.interval,
        candle_open_time=signal.candle_open_time,
        candle_close_time=signal.candle_close_time,
        action="REVERSE",
        current_position="LONG",
        target_position="SHORT",
        bullish_count=0,
        bearish_count=1,
        evidence=(evidence,),
        entry_signal=signal,
        reason="fixture reversal",
    )


class _FakeClient:
    def __init__(self):
        self.amount = Decimal("0.01")

    def positions(self, symbol, risk_reducing=False):
        if self.amount == 0:
            return []
        return [{"symbol": symbol, "positionAmt": str(self.amount)}]


class _FakeService:
    def __init__(self, journal):
        self.journal = journal
        self.client = _FakeClient()
        self.events = []

    def close_position(self, symbol):
        self.events.append("close")
        self.client.amount = Decimal(0)
        return {"close": {"status": "FILLED"}, "postClose": {"symbol": symbol}}

    def execute_intent(self, intent):
        self.events.append("entry")
        self.client.amount = Decimal("-0.01")
        return SimpleNamespace(as_dict=lambda: {"entry": {"status": "FILLED"}})


class _FakeRisk:
    def __init__(self):
        self.reasons = []

    def lock_entries(self, reason):
        self.reasons.append(reason)


class StrategyReversalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(Path(self.directory.name) / "orders.db")

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def payload(self):
        value = decision()
        intent = EntryIntent.create(
            source="test",
            symbol="BTCUSDT",
            side="SELL",
            risk_usdt=Decimal("1"),
            stop_price=Decimal("110"),
            take_profit_price=Decimal("70"),
            leverage=3,
            margin_utilization=Decimal("0.5"),
        )
        return {
            "symbol": "BTCUSDT",
            "decisionId": value.decision_id,
            "decision": value.as_dict(),
            "entryIntent": intent.as_dict(),
        }

    def queue(self, payload):
        return self.journal.enqueue_strategy_command(
            payload["decisionId"], "STRATEGY_REVERSE", payload
        )

    def test_daemon_closes_confirms_flat_then_enters_opposite(self):
        payload = self.payload()
        self.assertIsNotNone(self.queue(payload))
        service = _FakeService(self.journal)
        risk = _FakeRisk()
        daemon = TradingDaemon(SimpleNamespace(), ["BTCUSDT"], "5m")
        result = daemon._execute_command(
            "STRATEGY_REVERSE", payload, service, risk
        )
        self.assertEqual(service.events, ["close", "entry"])
        self.assertEqual(result["targetPosition"], "SHORT")
        self.assertEqual(
            self.journal.strategy_reversal(payload["decisionId"])["phase"],
            "ENTRY_CONFIRMED",
        )
        self.assertEqual(risk.reasons, [])

    def test_interrupted_reversal_command_is_requeued_for_idempotent_recovery(self):
        payload = self.payload()
        command_id = self.queue(payload)
        assert command_id is not None
        self.assertTrue(self.journal.mark_command_running(command_id))
        self.assertEqual(self.journal.recover_interrupted_strategy_reversals(), 1)
        pending = self.journal.pending_commands()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["command_type"], "STRATEGY_REVERSE")

    def test_daemon_strategy_exit_only_closes_the_existing_side(self):
        value = StrategyExitDecision(
            strategy="multi-divergence-reversal-v1",
            version="5",
            instance_id="divergence-test",
            symbol="BTCUSDT",
            interval="5m",
            candle_open_time=700_001,
            candle_close_time=1_000_000,
            current_position="LONG",
            bullish_count=0,
            bearish_count=0,
            evidence=(),
            reason="fixture exit",
        )
        payload = {
            "symbol": "BTCUSDT",
            "decisionId": value.decision_id,
            "decision": value.as_dict(),
        }
        service = _FakeService(self.journal)
        daemon = TradingDaemon(SimpleNamespace(), ["BTCUSDT"], "5m")
        result = daemon._execute_command(
            "STRATEGY_EXIT", payload, service, _FakeRisk()
        )
        self.assertEqual(service.events, ["close"])
        self.assertEqual(result["decisionId"], value.decision_id)


if __name__ == "__main__":
    unittest.main()
