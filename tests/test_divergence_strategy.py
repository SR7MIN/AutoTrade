import unittest
from decimal import Decimal

from autotrade.backtest import BacktestEngine
from autotrade.market_data import Candle
from autotrade.strategy.base import (
    DivergenceEvidence,
    StrategyDecision,
    StrategySignal,
)
from autotrade.strategy.divergence import (
    ConfirmedDivergenceDetector,
    DivergenceObservation,
)
from autotrade.strategy.multi_divergence import MultiDivergenceReversalStrategy


def candle(
    index: int,
    *,
    open_price: str = "100",
    high: str = "105",
    low: str = "95",
    close: str = "100",
) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="5m",
        open_time=index * 300_000,
        close_time=(index + 1) * 300_000 - 1,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume="10",
        trade_count=1,
        closed=True,
    )


def evidence(
    indicator: str, direction: str, divergence_type: str, current_price: str
) -> DivergenceEvidence:
    bullish = direction == "BULLISH"
    return DivergenceEvidence(
        indicator=indicator,
        divergence_type=divergence_type,
        direction=direction,
        current_pivot_time=300_000,
        previous_pivot_time=0,
        current_price=Decimal(current_price),
        previous_price=Decimal("95" if bullish else "105"),
        current_indicator=Decimal("60" if bullish else "40"),
        previous_indicator=Decimal("50"),
    )


class ConfirmedDivergenceDetectorTests(unittest.TestCase):
    def test_regular_and_hidden_bullish_divergences_wait_for_confirmation(self):
        detector = ConfirmedDivergenceDetector(
            indicator_names=("a", "b"),
            pivot_period=1,
            max_pivots=4,
            max_bars_to_check=10,
            divergence_types=("regular", "hidden"),
        )
        lows = ("9", "8", "10", "7", "11", "9", "11")
        indicator_values = (
            (Decimal("9"), Decimal("19")),
            (Decimal("10"), Decimal("20")),
            (Decimal("11"), Decimal("21")),
            (Decimal("12"), Decimal("22")),
            (Decimal("11"), Decimal("21")),
            (Decimal("10"), Decimal("20")),
            (Decimal("9"), Decimal("19")),
        )
        observations = []
        for index, low in enumerate(lows):
            value = detector.update(
                candle(index, high=str(Decimal(low) + 2), low=low, close=str(Decimal(low) + 1)),
                {"a": indicator_values[index][0], "b": indicator_values[index][1]},
            )
            observations.append(value)
        self.assertIsNone(observations[3])
        regular = observations[4]
        self.assertIsNotNone(regular)
        assert regular is not None
        self.assertEqual(regular.bullish_count, 2)
        self.assertEqual(
            {item.divergence_type for item in regular.bullish_evidence}, {"REGULAR"}
        )
        hidden = observations[6]
        self.assertIsNotNone(hidden)
        assert hidden is not None
        self.assertEqual(hidden.bullish_count, 2)
        self.assertIn("HIDDEN", {item.divergence_type for item in hidden.bullish_evidence})

    def test_count_uses_unique_indicators_across_regular_and_hidden(self):
        observation = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("rsi", "BULLISH", "HIDDEN", "90"),
                evidence("obv", "BULLISH", "REGULAR", "90"),
            ),
            bearish_evidence=(),
        )
        self.assertEqual(observation.bullish_count, 2)


class _FakeSuite:
    def update(self, value):
        return {name: Decimal("1") for name in (
            "rsi", "macd", "macd_histogram", "stochastic", "cci",
            "momentum", "obv", "vwmacd", "cmf",
        )}, Decimal("10")


class _FakeDetector:
    def __init__(self, observations):
        self.observations = list(observations)

    def update(self, value, indicators):
        return self.observations.pop(0) if self.observations else None


class MultiDivergenceStateMachineTests(unittest.TestCase):
    def strategy(self, observations) -> MultiDivergenceReversalStrategy:
        strategy = MultiDivergenceReversalStrategy(
            symbol="BTCUSDT", interval="5m", instance_id="div-test"
        )
        strategy._suite = _FakeSuite()  # type: ignore[attr-defined]
        strategy._detector = _FakeDetector(observations)  # type: ignore[attr-defined]
        return strategy

    def test_enters_then_reverses_on_two_opposite_indicators(self):
        bullish = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("obv", "BULLISH", "HIDDEN", "90"),
            ),
            bearish_evidence=(),
        )
        bearish = DivergenceObservation(
            candle_open_time=300_000,
            candle_close_time=599_999,
            bullish_evidence=(),
            bearish_evidence=(
                evidence("macd", "BEARISH", "REGULAR", "110"),
                evidence("cmf", "BEARISH", "HIDDEN", "110"),
            ),
        )
        strategy = self.strategy((bullish, bearish))
        strategy.set_position("FLAT")
        enter = strategy.on_candle(candle(0))
        self.assertIsNotNone(enter)
        assert enter is not None
        self.assertEqual((enter.action, enter.target_position), ("ENTER", "LONG"))
        self.assertEqual(enter.entry_signal.stop_price, Decimal("85.0"))
        self.assertEqual(enter.entry_signal.take_profit_price, Decimal("145.0"))
        strategy.set_position("LONG")
        reverse = strategy.on_candle(candle(1))
        self.assertIsNotNone(reverse)
        assert reverse is not None
        self.assertEqual((reverse.action, reverse.target_position), ("REVERSE", "SHORT"))

    def test_conflicting_two_sided_signal_holds(self):
        observation = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("obv", "BULLISH", "HIDDEN", "90"),
            ),
            bearish_evidence=(
                evidence("macd", "BEARISH", "REGULAR", "110"),
                evidence("cmf", "BEARISH", "HIDDEN", "110"),
            ),
        )
        strategy = self.strategy((observation,))
        strategy.set_position("FLAT")
        self.assertIsNone(strategy.on_candle(candle(0)))


class _FixedDecisionStrategy:
    name = "decision-fixture"
    version = "1"
    symbol = "BTCUSDT"
    interval = "5m"
    instance_id = "decision-fixture"

    def reset(self):
        self.position = "FLAT"

    def set_position(self, position):
        self.position = position

    def signal(self, value, side):
        return StrategySignal(
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

    def on_candle(self, value):
        index = value.open_time // 300_000
        if index == 0:
            signal = self.signal(value, "BUY")
            return StrategyDecision(
                strategy=self.name, version=self.version, instance_id=self.instance_id,
                symbol=value.symbol, interval=value.interval,
                candle_open_time=value.open_time, candle_close_time=value.close_time,
                action="ENTER", current_position="FLAT", target_position="LONG",
                bullish_count=1, bearish_count=0,
                evidence=(evidence("rsi", "BULLISH", "REGULAR", "90"),),
                entry_signal=signal, reason="enter",
            )
        if index == 2 and self.position == "LONG":
            signal = self.signal(value, "SELL")
            return StrategyDecision(
                strategy=self.name, version=self.version, instance_id=self.instance_id,
                symbol=value.symbol, interval=value.interval,
                candle_open_time=value.open_time, candle_close_time=value.close_time,
                action="REVERSE", current_position="LONG", target_position="SHORT",
                bullish_count=0, bearish_count=1,
                evidence=(evidence("macd", "BEARISH", "REGULAR", "110"),),
                entry_signal=signal, reason="reverse",
            )
        return None


class DivergenceBacktestTests(unittest.TestCase):
    def test_reversal_closes_then_opens_opposite_at_next_bar(self):
        values = [
            candle(0),
            candle(1),
            candle(2),
            candle(3),
            candle(4, high="105", low="79", close="82"),
        ]
        result = BacktestEngine(
            fee_bps=Decimal(0), slippage_bps=Decimal(0), cooldown_bars=0
        ).run(values, _FixedDecisionStrategy())
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[0].exit_reason, "REVERSE")
        self.assertEqual(result.trades[0].exit_time, values[3].open_time)
        self.assertEqual(result.trades[1].side, "SELL")
        self.assertEqual(result.trades[1].entry_time, values[3].open_time)
        self.assertEqual(result.trades[1].exit_reason, "TAKE_PROFIT")


if __name__ == "__main__":
    unittest.main()
