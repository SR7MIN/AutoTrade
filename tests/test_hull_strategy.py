import unittest
from decimal import Decimal

from autotrade.candles import Candle
from autotrade.strategy import (
    StrategyDecision,
    StrategyExitDecision,
    StrategySignal,
)
from autotrade.strategy.hull import (
    HullSuiteFullEquityStrategy,
    PineHullMovingAverage,
    PineWeightedMovingAverage,
)


DAY_MS = 86_400_000


def candle(index: int, close: int, *, interval: str = "1d") -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval=interval,
        open_time=index * DAY_MS,
        close_time=(index + 1) * DAY_MS - 1,
        open=str(close),
        high=str(close + 1),
        low=str(close - 1),
        close=str(close),
        volume="100",
        trade_count=1,
        closed=True,
    )


class HullIndicatorTests(unittest.TestCase):
    def test_weighted_moving_average_uses_oldest_to_newest_weights(self):
        wma = PineWeightedMovingAverage(3)
        self.assertIsNone(wma.update(Decimal(1)))
        self.assertIsNone(wma.update(Decimal(2)))
        self.assertEqual(wma.update(Decimal(3)), Decimal(14) / Decimal(6))
        self.assertEqual(wma.update(Decimal(4)), Decimal(20) / Decimal(6))

    def test_hma_55_uses_pine_v4_effective_lengths(self):
        hma = PineHullMovingAverage(55)
        self.assertEqual((hma.half_length, hma.sqrt_length), (27, 7))
        outputs = [hma.update(Decimal(index)) for index in range(1, 63)]
        self.assertTrue(all(value is None for value in outputs[:60]))
        self.assertIsNotNone(outputs[60])


class HullStrategyTests(unittest.TestCase):
    def strategy(self, **values) -> HullSuiteFullEquityStrategy:
        return HullSuiteFullEquityStrategy(
            symbol="BTCUSDT",
            interval="1d",
            instance_id="hull-test",
            length=10,
            structure_lookback=3,
            max_stop_atr=Decimal("10"),
            **values,
        )

    def test_emits_once_per_hull_direction_and_reverses_on_flip(self):
        strategy = self.strategy()
        closes = [*range(100, 125), *range(124, 90, -1)]
        outputs = []
        for index, close in enumerate(closes):
            output = strategy.on_candle(candle(index, close))
            if output is not None:
                outputs.append(output)
                if isinstance(output, StrategySignal):
                    strategy.set_position("LONG" if output.side == "BUY" else "SHORT")
        self.assertEqual(len(outputs), 2)
        self.assertIsInstance(outputs[0], StrategySignal)
        self.assertEqual(outputs[0].side, "BUY")
        self.assertIsNone(outputs[0].take_profit_price)
        self.assertIsNone(outputs[0].stop_price)
        self.assertEqual(
            dict(outputs[0].indicators)["position_size_mode"], "full_equity"
        )
        self.assertIsNone(StrategySignal.from_dict(outputs[0].as_dict()).stop_price)
        self.assertIsInstance(outputs[1], StrategyDecision)
        self.assertEqual((outputs[1].action, outputs[1].target_position), ("REVERSE", "SHORT"))

    def test_flat_after_stop_does_not_reenter_until_hull_changes(self):
        strategy = self.strategy()
        first = None
        for index, close in enumerate(range(100, 125)):
            output = strategy.on_candle(candle(index, close))
            first = first or output
        self.assertIsNotNone(first)
        strategy.set_position("FLAT")
        self.assertIsNone(strategy.on_candle(candle(25, 125)))

    def test_rejects_non_daily_candles(self):
        with self.assertRaisesRegex(ValueError, "daily"):
            HullSuiteFullEquityStrategy(symbol="BTCUSDT", interval="5m")

    def test_optional_protective_stop_can_be_enabled(self):
        strategy = self.strategy(protective_stop_enabled=True)
        signal = None
        for index, close in enumerate(range(100, 125)):
            signal = signal or strategy.on_candle(candle(index, close))
        self.assertIsInstance(signal, StrategySignal)
        self.assertIsNotNone(signal.stop_price)
        self.assertIn("max_stop_bps", dict(signal.indicators))

    def test_long_only_closes_instead_of_opening_short(self):
        strategy = self.strategy(direction="long")
        closes = [*range(100, 125), *range(124, 90, -1)]
        outputs = []
        for index, close in enumerate(closes):
            output = strategy.on_candle(candle(index, close))
            if output is not None:
                outputs.append(output)
                if isinstance(output, StrategySignal):
                    strategy.set_position("LONG")
        self.assertEqual(len(outputs), 2)
        self.assertIsInstance(outputs[-1], StrategyExitDecision)

    def test_short_only_direction_is_not_supported(self):
        with self.assertRaisesRegex(ValueError, "long or all"):
            self.strategy(direction="short")


if __name__ == "__main__":
    unittest.main()
