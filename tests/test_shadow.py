import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autotrade.journal import OrderJournal
from autotrade.market_data import Candle
from autotrade.shadow import ShadowRunner
from autotrade.strategy.base import (
    DivergenceEvidence,
    StrategyDecision,
    StrategySignal,
)
from autotrade.strategy.lifecycle_pulse import LifecyclePulseStrategy


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
    instance_id = "fixed-test"

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
            instance_id=self.instance_id,
        )


class FixedDecisionStrategy:
    name = "decision-fixed"
    version = "1"
    symbol = "BTCUSDT"
    interval = "5m"
    instance_id = "decision-fixed"

    def __init__(self):
        self.position = "FLAT"

    def reset(self):
        self.position = "FLAT"

    def set_position(self, position):
        self.position = position

    def _decision(self, value, *, side, action, current, target):
        direction = "BULLISH" if side == "BUY" else "BEARISH"
        signal = StrategySignal(
            strategy=self.name,
            version=self.version,
            symbol=value.symbol,
            interval=value.interval,
            candle_open_time=value.open_time,
            candle_close_time=value.close_time,
            side=side,
            reference_price=Decimal("100"),
            stop_price=Decimal("90" if side == "BUY" else "110"),
            take_profit_price=Decimal("120" if side == "BUY" else "80"),
            risk_usdt=Decimal("1"),
            leverage=3,
            margin_utilization=Decimal("0.5"),
            indicators=(),
            reason="fixture",
            instance_id=self.instance_id,
        )
        item = DivergenceEvidence(
            indicator="rsi",
            divergence_type="REGULAR",
            direction=direction,
            current_pivot_time=value.open_time,
            previous_pivot_time=max(0, value.open_time - 300_000),
            current_price=Decimal("90" if side == "BUY" else "110"),
            previous_price=Decimal("95" if side == "BUY" else "105"),
            current_indicator=Decimal("60" if side == "BUY" else "40"),
            previous_indicator=Decimal("50"),
        )
        return StrategyDecision(
            strategy=self.name,
            version=self.version,
            instance_id=self.instance_id,
            symbol=value.symbol,
            interval=value.interval,
            candle_open_time=value.open_time,
            candle_close_time=value.close_time,
            action=action,
            current_position=current,
            target_position=target,
            bullish_count=1 if side == "BUY" else 0,
            bearish_count=1 if side == "SELL" else 0,
            evidence=(item,),
            entry_signal=signal,
            reason="fixture",
        )

    def on_candle(self, value):
        index = value.open_time // 300_000
        if index == 1 and self.position == "FLAT":
            return self._decision(
                value, side="BUY", action="ENTER", current="FLAT", target="LONG"
            )
        if index == 2 and self.position == "LONG":
            return self._decision(
                value,
                side="SELL",
                action="REVERSE",
                current="LONG",
                target="SHORT",
            )
        return None


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
                    "instance_id": "fixed-test",
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

    def test_lifecycle_strategy_emits_first_new_bar_into_its_own_paths(self):
        root = Path(self.directory.name) / "strategies" / "lifecycle-pulse"
        runner = ShadowRunner(
            database_path=self.database,
            state_path=root / "state.json",
            log_path=root / "shadow.jsonl",
            cooldown_bars=1,
        )
        strategy = LifecyclePulseStrategy(
            symbol="BTCUSDT", interval="5m", instance_id="lifecycle-pulse"
        )
        self.store(candle(0))
        warmup = runner.run_once(strategy)
        self.assertEqual(warmup.signals_emitted, 0)
        self.store(candle(1, close="101"))
        result = runner.run_once(strategy)
        self.assertEqual(result.signals_emitted, 1)
        self.assertEqual(result.signals_accepted, 1)
        loaded = ShadowRunner.load_signal(root / "shadow.jsonl")
        self.assertEqual(loaded.instance_id, "lifecycle-pulse")
        self.assertEqual(loaded.candle_open_time, candle(1).open_time)

    def test_shadow_logs_and_replays_two_phase_reversal_decision(self):
        self.store(candle(0))
        self.runner.run_once(FixedDecisionStrategy())
        self.store(candle(1))
        entered = self.runner.run_once(FixedDecisionStrategy())
        self.assertEqual(entered.signals_accepted, 1)
        self.store(candle(2))
        reversed_result = self.runner.run_once(FixedDecisionStrategy())
        self.assertEqual(reversed_result.signals_accepted, 1)
        lines = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[-1]["decision"], "REVERSE_ACCEPTED")
        loaded = ShadowRunner.load_decision(self.log, lines[-1]["decisionId"])
        self.assertEqual(loaded.action, "REVERSE")
        self.assertEqual(loaded.target_position, "SHORT")


if __name__ == "__main__":
    unittest.main()
