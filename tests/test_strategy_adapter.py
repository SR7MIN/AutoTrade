import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autotrade.config import RiskSettings
from autotrade.errors import ConfigurationError, EntryPaused, RuleViolation
from autotrade.journal import OrderJournal
from autotrade.strategy.base import (
    DivergenceEvidence,
    StrategyDecision,
    StrategyExitDecision,
    StrategySignal,
)
from autotrade.strategy_adapter import TestnetStrategyAdapter
from autotrade.strategy_manager import StrategyInstanceConfig


def risk_settings():
    return RiskSettings(
        max_risk_usdt=Decimal("25"),
        max_risk_fraction=Decimal("0.01"),
        max_order_notional=Decimal("2500"),
        max_symbol_notional=Decimal("2500"),
        max_total_notional=Decimal("5000"),
        max_leverage=5,
        max_open_symbols=3,
        max_daily_loss=Decimal("100"),
        max_consecutive_losses=3,
        min_available_margin=Decimal("50"),
        min_liquidation_distance=Decimal("0.10"),
        fee_bps=Decimal("5"),
        slippage_bps=Decimal("10"),
        max_mark_age_seconds=10,
    )


def signal(close_time=1_000_000):
    return StrategySignal(
        strategy="ema-atr-v1",
        version="1",
        symbol="BTCUSDT",
        interval="5m",
        candle_open_time=close_time - 299_999,
        candle_close_time=close_time,
        side="BUY",
        reference_price=Decimal("100"),
        stop_price=Decimal("90"),
        take_profit_price=Decimal("120"),
        risk_usdt=Decimal("1"),
        leverage=3,
        margin_utilization=Decimal("0.5"),
        indicators=(),
        reason="fixture",
        instance_id="ema-default",
    )


class StrategyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.journal = OrderJournal(root / "orders.db")
        self.settings = SimpleNamespace(
            is_testnet=True,
            risk=risk_settings(),
            lock_path=root / "writer.lock",
        )
        self.instance = StrategyInstanceConfig(
            instance_id="ema-default",
            implementation="ema-atr-v1",
            enabled=True,
            symbol="BTCUSDT",
            interval="5m",
            parameters={},
        )
        self.adapter = TestnetStrategyAdapter(
            self.settings, self.journal, self.instance, "1"
        )

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def healthy(self):
        self.journal.set_control("entry_enabled", "true", "test")
        self.journal.set_control("user_stream_healthy", "true", "test")
        self.journal.set_control("market_data_BTCUSDT_5m_healthy", "true", "test")
        self.journal.set_control("active_strategy_instance", "ema-default", "test")

    def test_preview_requires_health_but_does_not_enqueue(self):
        self.journal.set_control("active_strategy_instance", "ema-default", "test")
        with self.assertRaises(EntryPaused):
            self.adapter.submit(signal(), execute=False, now_ms=1_001_000)
        self.healthy()
        result = self.adapter.submit(signal(), execute=False, now_ms=1_001_000)
        self.assertEqual(result.mode, "preview")
        self.assertEqual(self.journal.recent_commands(), [])

    def test_mainnet_risk_and_leverage_caps_are_enforced(self):
        self.healthy()
        mainnet = SimpleNamespace(
            is_testnet=False, risk=risk_settings(), lock_path=self.settings.lock_path
        )
        with self.assertRaises(ConfigurationError):
            TestnetStrategyAdapter(mainnet, self.journal, self.instance, "1").submit(
                signal(), execute=False, now_ms=1_001_000
            )
        with self.assertRaisesRegex(RuleViolation, "1 USDT"):
            self.adapter.submit(
                replace(signal(), risk_usdt=Decimal("1.01")),
                execute=False,
                now_ms=1_001_000,
            )
        with self.assertRaisesRegex(RuleViolation, "3x"):
            self.adapter.submit(
                replace(signal(), leverage=4), execute=False, now_ms=1_001_000
            )

    def test_testnet_only_registration_is_explicitly_rejected_on_mainnet(self):
        self.healthy()
        mainnet = SimpleNamespace(
            is_testnet=False, risk=risk_settings(), lock_path=self.settings.lock_path
        )
        with self.assertRaisesRegex(ConfigurationError, "restricted to Testnet"):
            TestnetStrategyAdapter(
                mainnet,
                self.journal,
                self.instance,
                "1",
                testnet_only=True,
            ).submit(signal(), execute=False, now_ms=1_001_000)

    def test_execute_requires_daemon_and_rejects_duplicate_signal(self):
        self.healthy()
        with patch("autotrade.strategy_adapter.lock_owner_active", return_value=False):
            with self.assertRaisesRegex(RuleViolation, "running daemon"):
                self.adapter.submit(signal(), execute=True, now_ms=1_001_000)
        with patch("autotrade.strategy_adapter.lock_owner_active", return_value=True):
            result = self.adapter.submit(signal(), execute=True, now_ms=1_001_000)
            self.assertEqual(result.mode, "queued")
            self.assertIsNotNone(result.command_id)
            command = self.journal.pending_commands()[0]
            self.assertEqual(command["command_type"], "ENTRY_INTENT")
            with self.assertRaisesRegex(RuleViolation, "already been submitted"):
                self.adapter.submit(signal(), execute=True, now_ms=1_001_000)

    def test_stale_signal_is_rejected(self):
        self.healthy()
        with self.assertRaisesRegex(RuleViolation, "stale"):
            self.adapter.submit(signal(), execute=False, now_ms=1_100_001)

    def test_inactive_strategy_instance_is_rejected(self):
        self.healthy()
        fast_instance = replace(self.instance, instance_id="ema-fast")
        adapter = TestnetStrategyAdapter(
            self.settings, self.journal, fast_instance, "1"
        )
        with self.assertRaisesRegex(RuleViolation, "not active"):
            adapter.submit(
                replace(signal(), instance_id="ema-fast"),
                execute=False,
                now_ms=1_001_000,
            )

    def test_reversal_decision_queues_two_phase_strategy_command(self):
        instance = replace(
            self.instance,
            instance_id="divergence-test",
            implementation="multi-divergence-reversal-v1",
        )
        self.journal.set_control("entry_enabled", "true", "test")
        self.journal.set_control("user_stream_healthy", "true", "test")
        self.journal.set_control("market_data_BTCUSDT_5m_healthy", "true", "test")
        self.journal.set_control("active_strategy_instance", "divergence-test", "test")
        intent_id = self.journal.create_intent(
            client_order_id="active-entry",
            symbol="BTCUSDT",
            side="BUY",
            quantity="0.01",
            stop_price="90",
            take_profit_price="120",
            details={},
        )
        self.journal.update(intent_id, "PROTECTED")
        self.journal.record_order(
            {
                "symbol": "BTCUSDT",
                "clientAlgoId": "active-stop",
                "algoId": "1",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "quantity": "0.01",
                "reduceOnly": True,
            },
            family="ALGO",
            role="STOP",
            intent_id=intent_id,
        )
        value = replace(
            signal(),
            strategy="multi-divergence-reversal-v1",
            side="SELL",
            stop_price=Decimal("110"),
            take_profit_price=Decimal("80"),
            instance_id="divergence-test",
        )
        evidence = DivergenceEvidence(
            indicator="rsi",
            divergence_type="REGULAR",
            direction="BEARISH",
            current_pivot_time=900_000,
            previous_pivot_time=600_000,
            current_price=Decimal("105"),
            previous_price=Decimal("104"),
            current_indicator=Decimal("60"),
            previous_indicator=Decimal("70"),
        )
        decision = StrategyDecision(
            strategy=value.strategy,
            version=value.version,
            instance_id="divergence-test",
            symbol=value.symbol,
            interval=value.interval,
            candle_open_time=value.candle_open_time,
            candle_close_time=value.candle_close_time,
            action="REVERSE",
            current_position="LONG",
            target_position="SHORT",
            bullish_count=0,
            bearish_count=1,
            evidence=(evidence,),
            entry_signal=value,
            reason="fixture",
        )
        adapter = TestnetStrategyAdapter(
            self.settings, self.journal, instance, "1"
        )
        preview = adapter.submit_decision(
            decision, execute=False, now_ms=1_001_000
        )
        self.assertEqual(preview.action, "REVERSE")
        with patch("autotrade.strategy_adapter.lock_owner_active", return_value=True):
            queued = adapter.submit_decision(
                decision, execute=True, now_ms=1_001_000
            )
        command = self.journal.pending_commands()[0]
        self.assertEqual(command["command_type"], "STRATEGY_REVERSE")
        self.assertEqual(queued.decision_id, decision.decision_id)
        self.assertEqual(
            self.journal.strategy_reversal(decision.decision_id)["phase"], "QUEUED"
        )

    def test_exit_decision_queues_risk_reducing_command_without_entry_intent(self):
        instance = replace(
            self.instance,
            instance_id="divergence-test",
            implementation="multi-divergence-reversal-v1",
        )
        self.journal.set_control("user_stream_healthy", "true", "test")
        self.journal.set_control("market_data_BTCUSDT_5m_healthy", "true", "test")
        self.journal.set_control("active_strategy_instance", "divergence-test", "test")
        intent_id = self.journal.create_intent(
            client_order_id="exit-active-entry",
            symbol="BTCUSDT",
            side="BUY",
            quantity="0.01",
            stop_price="90",
            take_profit_price=None,
            details={},
        )
        self.journal.update(intent_id, "PROTECTED")
        self.journal.record_order(
            {
                "symbol": "BTCUSDT",
                "clientAlgoId": "exit-active-stop",
                "algoId": "2",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "quantity": "0.01",
                "reduceOnly": True,
            },
            family="ALGO",
            role="STOP",
            intent_id=intent_id,
        )
        decision = StrategyExitDecision(
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
        adapter = TestnetStrategyAdapter(self.settings, self.journal, instance, "5")
        preview = adapter.submit_decision(decision, execute=False, now_ms=1_001_000)
        self.assertEqual(preview.action, "EXIT")
        self.assertIsNone(preview.intent)
        with patch("autotrade.strategy_adapter.lock_owner_active", return_value=True):
            queued = adapter.submit_decision(decision, execute=True, now_ms=1_001_000)
        command = self.journal.pending_commands()[0]
        self.assertEqual(command["command_type"], "STRATEGY_EXIT")
        self.assertEqual(queued.decision_id, decision.decision_id)

    def test_research_only_strategy_is_rejected_before_preview(self):
        self.healthy()
        adapter = TestnetStrategyAdapter(
            self.settings,
            self.journal,
            self.instance,
            "1",
            research_only=True,
        )
        with self.assertRaisesRegex(RuleViolation, "research-only"):
            adapter.submit(signal(), execute=False, now_ms=1_001_000)


if __name__ == "__main__":
    unittest.main()
