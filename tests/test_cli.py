import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autotrade.cli import build_parser, run, timestamp_argument
from autotrade.errors import InstanceLockError
from autotrade.journal import OrderJournal
from autotrade.market_data import Candle


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


if __name__ == "__main__":
    unittest.main()
