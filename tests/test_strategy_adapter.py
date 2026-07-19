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
from autotrade.strategy.base import StrategySignal
from autotrade.strategy_adapter import TestnetStrategyAdapter


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
        self.adapter = TestnetStrategyAdapter(self.settings, self.journal)

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def healthy(self):
        self.journal.set_control("entry_enabled", "true", "test")
        self.journal.set_control("user_stream_healthy", "true", "test")
        self.journal.set_control("market_data_BTCUSDT_5m_healthy", "true", "test")

    def test_preview_requires_health_but_does_not_enqueue(self):
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
            TestnetStrategyAdapter(mainnet, self.journal).submit(
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


if __name__ == "__main__":
    unittest.main()
