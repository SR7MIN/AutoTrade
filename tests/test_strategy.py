import ast
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import autotrade.strategy
from autotrade.market_data import Candle
from autotrade.strategy.ema_atr import EmaAtrStrategy
from autotrade.strategy.indicators import ExponentialMovingAverage, WilderAverageTrueRange


def candle(index: int, close: str, *, closed: bool = True) -> Candle:
    price = Decimal(close)
    return Candle(
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
        closed=closed,
    )


class IndicatorTests(unittest.TestCase):
    def test_ema_seeds_with_sma_then_updates(self) -> None:
        ema = ExponentialMovingAverage(3)
        self.assertIsNone(ema.update(Decimal("1")))
        self.assertIsNone(ema.update(Decimal("2")))
        self.assertEqual(ema.update(Decimal("3")), Decimal("2"))
        self.assertEqual(ema.update(Decimal("4")), Decimal("3"))

    def test_wilder_atr_uses_true_range(self) -> None:
        atr = WilderAverageTrueRange(3)
        self.assertIsNone(atr.update(Decimal("11"), Decimal("9"), Decimal("10")))
        self.assertIsNone(atr.update(Decimal("13"), Decimal("11"), Decimal("12")))
        self.assertEqual(
            atr.update(Decimal("15"), Decimal("13"), Decimal("14")),
            Decimal("8") / Decimal("3"),
        )


class EmaAtrStrategyTests(unittest.TestCase):
    def strategy(self) -> EmaAtrStrategy:
        return EmaAtrStrategy(
            symbol="BTCUSDT", interval="5m", fast_period=2, slow_period=3, atr_period=2
        )

    def test_emits_immutable_long_signal_on_upward_cross(self) -> None:
        strategy = self.strategy()
        signals = [
            strategy.on_candle(candle(index, str(close)))
            for index, close in enumerate((130, 120, 110, 120, 130))
        ]
        signal = signals[-1]
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "BUY")
        self.assertLess(signal.stop_price, signal.reference_price)
        self.assertGreater(signal.take_profit_price, signal.reference_price)
        with self.assertRaises(FrozenInstanceError):
            signal.side = "SELL"  # type: ignore[misc]

    def test_emits_short_signal_on_downward_cross(self) -> None:
        strategy = self.strategy()
        signal = None
        for index, close in enumerate((110, 120, 130, 120, 110)):
            signal = strategy.on_candle(candle(index, str(close)))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "SELL")
        self.assertLess(signal.take_profit_price, signal.reference_price)
        self.assertGreater(signal.stop_price, signal.reference_price)

    def test_rejects_open_or_non_monotonic_candles(self) -> None:
        strategy = self.strategy()
        with self.assertRaisesRegex(ValueError, "closed candles"):
            strategy.on_candle(candle(0, "100", closed=False))
        strategy.on_candle(candle(0, "100"))
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            strategy.on_candle(candle(0, "101"))

    def test_strategy_package_has_no_execution_dependencies(self) -> None:
        forbidden = {"binance_rest", "config", "journal", "trading"}
        package = Path(autotrade.strategy.__file__).parent
        paths = [*package.glob("*.py"), package.parent / "candles.py"]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = {
                node.module.split(".")[-1]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            imported.update(
                alias.name.split(".")[-1]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            self.assertFalse(forbidden & imported, path.name)


if __name__ == "__main__":
    unittest.main()
