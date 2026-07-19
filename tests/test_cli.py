import tempfile
import unittest
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autotrade.cli import build_parser, run, timestamp_argument
from autotrade.config import RiskSettings
from autotrade.errors import InstanceLockError, RuleViolation
from autotrade.journal import OrderJournal
from autotrade.market_data import Candle
from autotrade.strategy.base import StrategySignal


class CliResearchTests(unittest.TestCase):
    def test_timestamp_argument_accepts_utc_date_and_epoch_milliseconds(self) -> None:
        self.assertEqual(timestamp_argument("1970-01-01"), 0)
        self.assertEqual(timestamp_argument("1234"), 1234)

    def test_replay_strategy_runs_offline_against_selected_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "research.db"
            journal = OrderJournal(database)
            try:
                for index in range(60):
                    price = 100 + (index % 10)
                    journal.store_candle(
                        Candle(
                            symbol="BTCUSDT",
                            interval="5m",
                            open_time=index * 300_000,
                            close_time=(index + 1) * 300_000 - 1,
                            open=str(price),
                            high=str(price + 1),
                            low=str(price - 1),
                            close=str(price),
                            volume="10",
                            trade_count=1,
                            closed=True,
                        ).as_dict()
                    )
            finally:
                journal.close()

            args = build_parser().parse_args(
                [
                    "replay-strategy",
                    "--symbol", "BTCUSDT",
                    "--interval", "5m",
                    "--database", str(database),
                ]
            )
            with patch("autotrade.cli.print_json") as print_json:
                self.assertEqual(run(args), 0)
            payload = print_json.call_args.args[0]
            self.assertEqual(payload["candleCount"], 60)
            self.assertEqual(payload["strategy"], "ema-atr-v1")
            self.assertEqual(payload["database"], str(database))

    def test_live_backfill_is_blocked_while_daemon_owns_writer_lock(self) -> None:
        class ClientContext:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                database_path=root / "orders.db",
                lock_path=root / "writer.lock",
                rest_url="https://example.invalid",
                ws_url="wss://example.invalid",
            )
            args = build_parser().parse_args(
                ["backfill", "--symbol", "BTCUSDT", "--interval", "1m"]
            )
            with (
                patch("autotrade.cli.Settings.from_env", return_value=settings),
                patch("autotrade.cli.BinanceRestClient", return_value=ClientContext()),
                patch("autotrade.cli.lock_owner_active", return_value=True),
            ):
                with self.assertRaisesRegex(InstanceLockError, "daemon is active"):
                    run(args)

    def test_shadow_once_warms_up_without_writing_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "market.db"
            state = root / "shadow-state.json"
            log = root / "shadow.jsonl"
            journal = OrderJournal(database)
            try:
                for index in range(60):
                    price = 100 + index % 10
                    journal.store_candle(
                        Candle(
                            symbol="BTCUSDT", interval="5m",
                            open_time=index * 300_000,
                            close_time=(index + 1) * 300_000 - 1,
                            open=str(price), high=str(price + 1), low=str(price - 1),
                            close=str(price), volume="10", trade_count=1, closed=True,
                        ).as_dict()
                    )
            finally:
                journal.close()
            args = build_parser().parse_args(
                [
                    "shadow", "--strategy", "ema-atr-v1", "--symbol", "BTCUSDT",
                    "--interval", "5m", "--database", str(database),
                    "--state", str(state), "--log", str(log), "--once",
                ]
            )
            with patch("autotrade.cli.print_json") as print_json:
                self.assertEqual(run(args), 0)
            self.assertEqual(print_json.call_args.args[0]["signalsEmitted"], 0)
            self.assertTrue(state.exists())
            self.assertFalse(log.exists())

    def test_submit_execute_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "shadow.jsonl"
            signal = StrategySignal(
                strategy="ema-atr-v1", version="1", symbol="BTCUSDT", interval="5m",
                candle_open_time=1, candle_close_time=2, side="BUY",
                reference_price=Decimal("100"), stop_price=Decimal("90"),
                take_profit_price=Decimal("120"), risk_usdt=Decimal("1"), leverage=3,
                margin_utilization=Decimal("0.5"), indicators=(), reason="test",
                instance_id="ema-default",
            )
            log.write_text(
                json.dumps(
                    {
                        "event": "SHADOW_SIGNAL", "decision": "ACCEPTED",
                        "signalId": "id", "signal": signal.as_dict(),
                    }
                ),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "submit-strategy", "--instance", "ema-default",
                    "--log", str(log), "--execute",
                ]
            )
            with self.assertRaisesRegex(RuleViolation, "confirm-testnet"):
                run(args)

    def test_submit_preview_loads_only_accepted_shadow_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "shadow.jsonl"
            database = root / "orders.db"
            signal = StrategySignal(
                strategy="ema-atr-v1", version="1", symbol="BTCUSDT", interval="5m",
                candle_open_time=700_001, candle_close_time=1_000_000, side="BUY",
                reference_price=Decimal("100"), stop_price=Decimal("90"),
                take_profit_price=Decimal("120"), risk_usdt=Decimal("1"), leverage=3,
                margin_utilization=Decimal("0.5"), indicators=(), reason="test",
                instance_id="ema-default",
            )
            log.write_text(
                json.dumps(
                    {
                        "event": "SHADOW_SIGNAL", "decision": "ACCEPTED",
                        "signalId": signal.signal_id, "signal": signal.as_dict(),
                    }
                ),
                encoding="utf-8",
            )
            journal = OrderJournal(database)
            journal.set_control("entry_enabled", "true", "test")
            journal.set_control("user_stream_healthy", "true", "test")
            journal.set_control("market_data_BTCUSDT_5m_healthy", "true", "test")
            journal.set_control("active_strategy_instance", "ema-default", "test")
            journal.close()
            settings = SimpleNamespace(
                is_testnet=True,
                risk=RiskSettings.from_env(),
                lock_path=root / "writer.lock",
                database_path=database,
                strategy_config_path=Path("strategies.toml"),
                strategy_state_dir=root / "strategies",
            )
            args = build_parser().parse_args(
                [
                    "submit-strategy", "--instance", "ema-default",
                    "--log", str(log),
                ]
            )
            with (
                patch("autotrade.cli.Settings.from_env", return_value=settings),
                patch("autotrade.strategy_adapter.time.time", return_value=1001),
                patch("autotrade.cli.print_json") as print_json,
            ):
                self.assertEqual(run(args), 0)
            self.assertEqual(print_json.call_args.args[0]["mode"], "preview")
            journal = OrderJournal(database)
            try:
                self.assertEqual(journal.recent_commands(), [])
            finally:
                journal.close()

    def test_strategy_instance_can_be_listed_activated_and_deactivated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "orders.db"
            config = root / "strategies.toml"
            config.write_text(
                """
[instances.ema-test]
implementation = "ema-atr-v1"
enabled = true
symbol = "BTCUSDT"
interval = "5m"

[instances.ema-test.parameters]
fast_period = 10
slow_period = 30
""",
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                database_path=database,
                strategy_config_path=config,
                strategy_state_dir=root / "strategies",
            )
            list_args = build_parser().parse_args(
                ["strategies", "--config", str(config)]
            )
            with (
                patch("autotrade.cli.Settings.from_env", return_value=settings),
                patch("autotrade.cli.print_json") as print_json,
            ):
                self.assertEqual(run(list_args), 0)
            self.assertEqual(
                print_json.call_args.args[0]["instances"][0]["instanceId"], "ema-test"
            )

            activate = build_parser().parse_args(
                [
                    "activate-strategy", "--instance", "ema-test",
                    "--config", str(config), "--reason", "test activation",
                ]
            )
            with (
                patch("autotrade.cli.Settings.from_env", return_value=settings),
                patch("autotrade.cli.print_json") as print_json,
            ):
                self.assertEqual(run(activate), 0)
            self.assertEqual(
                print_json.call_args.args[0]["activeExecutionInstance"], "ema-test"
            )
            self.assertFalse(print_json.call_args.args[0]["entryEnabled"])

            deactivate = build_parser().parse_args(
                ["deactivate-strategy", "--reason", "test complete"]
            )
            with (
                patch("autotrade.cli.Settings.from_env", return_value=settings),
                patch("autotrade.cli.print_json") as print_json,
            ):
                self.assertEqual(run(deactivate), 0)
            self.assertIsNone(
                print_json.call_args.args[0]["activeExecutionInstance"]
            )


if __name__ == "__main__":
    unittest.main()
