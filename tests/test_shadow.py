import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autotrade.journal import OrderJournal
from autotrade.market_data import Candle
from autotrade.shadow import ShadowRunner
from autotrade.strategy.base import StrategySignal


def candle(index, *, high="105", low="95", close="100"):
    return Candle(
        symbol="BTCUSDT",
        interval="5m",
        open_time=index * 300_000,
        close_time=(index + 1) * 300_000 - 1,
        open="100",
        high=high,
        low=low,
        close=close,
        volume="10",
        trade_count=1,
        closed=True,
    )


class FixedStrategy:
    name = "fixed"
    version = "1"
    symbol = "BTCUSDT"
    interval = "5m"

    def __init__(self, indexes):
        self.indexes = set(indexes)

    def reset(self):
        return None

    def on_candle(self, value):
        index = value.open_time // 300_000
        if index not in self.indexes:
            return None
        return StrategySignal(
            strategy=self.name,
            version=self.version,
            symbol=value.symbol,
            interval=value.interval,
            candle_open_time=value.open_time,
            candle_close_time=value.close_time,
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


class ShadowRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "market.db"
        self.state = root / "shadow-state.json"
        self.log = root / "shadow.jsonl"
        self.journal = OrderJournal(self.database)
        self.runner = ShadowRunner(
            database_path=self.database,
            state_path=self.state,
            log_path=self.log,
            cooldown_bars=3,
        )

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def store(self, *values):
        for value in values:
            self.journal.store_candle(value.as_dict())

    def test_initial_run_warms_up_without_emitting_historical_signals(self):
        self.store(candle(0), candle(1), candle(2))
        result = self.runner.run_once(FixedStrategy({1}))
        self.assertEqual(result.signals_emitted, 0)
        self.assertIsNone(result.virtual_position)
        self.assertIsNone(result.pending_entry)
        self.assertFalse(self.log.exists())
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["last_open_time"], candle(2).open_time)
        self.assertEqual(state["started_after_open_time"], candle(2).open_time)

    def test_new_signal_is_logged_once_and_state_recovers(self):
        self.store(candle(0), candle(1))
        self.runner.run_once(FixedStrategy(set()))
        self.store(candle(2))
        first = self.runner.run_once(FixedStrategy({2}))
        self.assertEqual(first.signals_emitted, 1)
        self.assertEqual(first.signals_accepted, 1)
        self.assertIsNotNone(first.pending_entry)
        signal_id = json.loads(self.log.read_text(encoding="utf-8"))["signalId"]
        loaded = ShadowRunner.load_signal(self.log, signal_id)
        self.assertEqual(loaded.candle_open_time, candle(2).open_time)

        repeated = self.runner.run_once(FixedStrategy({2}))
        self.assertEqual(repeated.signals_emitted, 0)
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)

    def test_virtual_position_and_cooldown_are_replayed_after_restart(self):
        self.store(candle(0), candle(1))
        self.runner.run_once(FixedStrategy(set()))
        self.store(candle(2))
        self.runner.run_once(FixedStrategy({2}))
        self.store(candle(3, high="105", low="89"))
        result = ShadowRunner(
            database_path=self.database,
            state_path=self.state,
            log_path=self.log,
            cooldown_bars=3,
        ).run_once(FixedStrategy({2}))
        self.assertIsNone(result.virtual_position)
        self.assertEqual(result.cooldown_bars_remaining, 3)

    def test_state_mismatch_is_rejected(self):
        self.store(candle(0))
        self.state.write_text(
            json.dumps(
                {
                    "strategy": "other",
                    "version": "1",
                    "symbol": "BTCUSDT",
                    "interval": "5m",
                    "last_open_time": 0,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "strategy does not match"):
            self.runner.run_once(FixedStrategy(set()))

    def test_tampered_signal_id_is_rejected(self):
        value = FixedStrategy({0}).on_candle(candle(0))
        assert value is not None
        self.log.write_text(
            json.dumps(
                {
                    "event": "SHADOW_SIGNAL",
                    "decision": "ACCEPTED",
                    "signalId": "tampered",
                    "signal": value.as_dict(),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            ShadowRunner.load_signal(self.log)


if __name__ == "__main__":
    unittest.main()
