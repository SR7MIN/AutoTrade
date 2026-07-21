import unittest
from decimal import Decimal

from autotrade.backtest import BacktestEngine
from autotrade.market_data import Candle
from autotrade.strategy.base import (
    DivergenceEvidence,
    StrategyDecision,
    StrategyExitDecision,
    StrategySignal,
)
from autotrade.strategy.divergence import (
    ConfirmedDivergenceDetector,
    DivergenceObservation,
)
from autotrade.strategy.multi_divergence import (
    MultiDivergenceReversalStrategy,
    _HourlyEmaTrend,
)
from autotrade.strategy.indicators import (
    MoneyFlowIndex,
    MovingAverageConvergenceDivergence,
    WilderDirectionalMovementIndex,
)


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
    indicator: str,
    direction: str,
    divergence_type: str,
    current_price: str,
    *,
    current_pivot_time: int = 300_000,
    previous_pivot_time: int = 0,
) -> DivergenceEvidence:
    bullish = direction == "BULLISH"
    return DivergenceEvidence(
        indicator=indicator,
        divergence_type=divergence_type,
        direction=direction,
        current_pivot_time=current_pivot_time,
        previous_pivot_time=previous_pivot_time,
        current_price=Decimal(current_price),
        previous_price=Decimal("95" if bullish else "105"),
        current_indicator=Decimal("60" if bullish else "40"),
        previous_indicator=Decimal("50"),
    )


class ConfirmedDivergenceDetectorTests(unittest.TestCase):
    def _detect(
        self,
        *,
        lows: tuple[str, ...],
        highs: tuple[str, ...],
        closes: tuple[str, ...],
        values: tuple[str, ...],
        divergence_type: str,
    ) -> DivergenceObservation | None:
        detector = ConfirmedDivergenceDetector(
            indicator_names=("a",),
            pivot_period=1,
            max_pivots=4,
            max_bars_to_check=30,
            divergence_types=(divergence_type,),
        )
        observation = None
        for index in range(len(closes)):
            observation = detector.update(
                candle(
                    index,
                    high=highs[index],
                    low=lows[index],
                    close=closes[index],
                ),
                {"a": Decimal(values[index])},
            )
        return observation

    def test_pine_confirmed_mode_evaluates_every_bar_with_previous_bar_endpoint(self):
        detector = ConfirmedDivergenceDetector(
            indicator_names=("a",),
            pivot_period=1,
            max_pivots=4,
            max_bars_to_check=30,
            divergence_types=("regular",),
        )
        lows = ("12", "10", "12", "12", "12", "12", "9", "11")
        closes = ("13", "11", "12", "12", "12", "12", "10", "11")
        indicator_values = ("12", "10", "16", "16", "16", "16", "15", "16")
        observations = []
        for index, low in enumerate(lows):
            value = detector.update(
                candle(index, high=str(Decimal(low) + 3), low=low, close=closes[index]),
                {"a": Decimal(indicator_values[index])},
            )
            observations.append(value)
        self.assertTrue(all(item is None for item in observations[:7]))
        regular = observations[7]
        self.assertIsNotNone(regular)
        assert regular is not None
        self.assertEqual(regular.bullish_count, 1)
        item = regular.bullish_evidence[0]
        self.assertEqual(item.divergence_type, "REGULAR")
        self.assertEqual(item.previous_pivot_time, 300_000)
        self.assertEqual(item.current_pivot_time, 6 * 300_000)

    def test_pine_regular_and_hidden_conditions_cover_both_directions(self):
        bullish_hidden = self._detect(
            lows=("12", "10", "12", "12", "12", "12", "11", "12"),
            highs=("15", "13", "15", "15", "15", "15", "14", "15"),
            closes=("13", "11", "13", "13", "13", "13", "12", "13"),
            values=("12", "15", "16", "16", "16", "16", "10", "11"),
            divergence_type="hidden",
        )
        self.assertIsNotNone(bullish_hidden)
        assert bullish_hidden is not None
        self.assertEqual(
            [(item.direction, item.divergence_type) for item in bullish_hidden.bullish_evidence],
            [("BULLISH", "HIDDEN")],
        )

        bearish_regular = self._detect(
            lows=("15", "17", "15", "15", "15", "15", "18", "17"),
            highs=("18", "20", "18", "18", "18", "18", "21", "20"),
            closes=("17", "19", "18", "18", "18", "18", "20", "19"),
            values=("18", "20", "14", "14", "14", "14", "15", "14"),
            divergence_type="regular",
        )
        self.assertIsNotNone(bearish_regular)
        assert bearish_regular is not None
        self.assertEqual(
            [(item.direction, item.divergence_type) for item in bearish_regular.bearish_evidence],
            [("BEARISH", "REGULAR")],
        )

        bearish_hidden = self._detect(
            lows=("15", "17", "15", "15", "15", "15", "16", "15"),
            highs=("18", "20", "18", "18", "18", "18", "19", "18"),
            closes=("17", "19", "17", "17", "17", "17", "18", "17"),
            values=("18", "20", "19", "19", "19", "19", "25", "24"),
            divergence_type="hidden",
        )
        self.assertIsNotNone(bearish_hidden)
        assert bearish_hidden is not None
        self.assertEqual(
            [(item.direction, item.divergence_type) for item in bearish_hidden.bearish_evidence],
            [("BEARISH", "HIDDEN")],
        )

    def test_pine_dontconfirm_mode_uses_current_bar_as_endpoint(self):
        detector = ConfirmedDivergenceDetector(
            indicator_names=("a",),
            pivot_period=1,
            max_pivots=4,
            max_bars_to_check=30,
            divergence_types=("regular",),
            require_confirmation=False,
        )
        lows = ("12", "10", "12", "12", "12", "12", "12", "9")
        values = ("12", "10", "16", "16", "16", "16", "16", "15")
        observation = None
        for index, low in enumerate(lows):
            observation = detector.update(
                candle(
                    index,
                    high=str(Decimal(low) + 3),
                    low=low,
                    close=str(Decimal(low) + 1),
                ),
                {"a": Decimal(values[index])},
            )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.bullish_evidence[0].current_pivot_time, 7 * 300_000)

    def test_pine_virtual_line_rejects_indicator_cut_through(self):
        observation = self._detect(
            lows=("12", "10", "12", "12", "12", "12", "9", "11"),
            highs=("15", "13", "15", "15", "15", "15", "12", "14"),
            closes=("13", "11", "12", "12", "12", "12", "10", "11"),
            values=("12", "10", "16", "16", "11", "16", "15", "16"),
            divergence_type="regular",
        )
        self.assertIsNone(observation)

    def test_count_matches_pine_indicator_type_slots(self):
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
        self.assertEqual(observation.bullish_count, 3)


class PineIndicatorParityTests(unittest.TestCase):
    def test_macd_ema_is_seeded_from_first_source_value(self):
        macd = MovingAverageConvergenceDivergence(12, 26, 9)
        self.assertEqual(macd.update(Decimal("10")), (Decimal(0), Decimal(0), Decimal(0)))
        line, signal, histogram = macd.update(Decimal("12"))
        expected_line = Decimal(4) / Decimal(13) - Decimal(4) / Decimal(27)
        assert line is not None and signal is not None and histogram is not None
        tolerance = Decimal("1e-25")
        self.assertLess(abs(line - expected_line), tolerance)
        self.assertLess(abs(signal - expected_line / Decimal(5)), tolerance)
        self.assertLess(
            abs(histogram - expected_line * Decimal(4) / Decimal(5)), tolerance
        )

    def test_mfi_uses_close_as_the_pine_source(self):
        mfi = MoneyFlowIndex(3)
        self.assertIsNone(mfi.update(Decimal("10"), Decimal(1)))
        self.assertIsNone(mfi.update(Decimal("10"), Decimal(1)))
        mfi.update(Decimal("11"), Decimal(1))
        expected = Decimal(100) - Decimal(100) / (Decimal(1) + Decimal(11) / Decimal(10))
        self.assertEqual(mfi.update(Decimal("10"), Decimal(1)), expected)

    def test_adx_returns_none_until_wilder_seed_is_complete(self):
        adx = WilderDirectionalMovementIndex(3)
        values = [
            ("101", "99", "100"),
            ("102", "99", "101"),
            ("103", "100", "102"),
            ("104", "101", "103"),
            ("105", "102", "104"),
            ("106", "103", "105"),
        ]
        output = [adx.update(*map(Decimal, item)) for item in values]
        self.assertTrue(all(item[0] is None for item in output[:4]))
        self.assertIsNotNone(output[-1][0])


class _FakeSuite:
    last_adx = None
    last_volume_ratio = None

    def update(self, value):
        return {name: Decimal("1") for name in (
            "rsi", "macd", "macd_histogram", "stochastic", "cci",
            "momentum", "obv", "vwmacd", "cmf",
            "mfi",
        )}, Decimal("10")


class _MetricSuite(_FakeSuite):
    last_adx = Decimal("25")
    last_volume_ratio = Decimal("1")


class _FakeDetector:
    def __init__(self, observations):
        self.observations = list(observations)

    def update(self, value, indicators):
        return self.observations.pop(0) if self.observations else None


class _FakeTrend:
    def __init__(self, state):
        self.state = state

    def update(self, value):
        return self.state


class MultiDivergenceStateMachineTests(unittest.TestCase):
    def strategy(self, observations) -> MultiDivergenceReversalStrategy:
        strategy = MultiDivergenceReversalStrategy(
            symbol="BTCUSDT",
            interval="5m",
            instance_id="div-test",
            min_entry_divergences=2,
            min_entry_indicator_groups=1,
            trend_filter_enabled=False,
            structure_break_enabled=False,
            min_stop_atr=Decimal("0.5"),
            market_filter_enabled=False,
            position_management_enabled=False,
        )
        strategy._suite = _FakeSuite()  # type: ignore[attr-defined]
        strategy._detector = _FakeDetector(observations)  # type: ignore[attr-defined]
        return strategy

    def test_enters_then_exits_and_waits_for_confirmed_reentry(self):
        bullish = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("obv", "BULLISH", "REGULAR", "90"),
            ),
            bearish_evidence=(),
        )
        bearish = DivergenceObservation(
            candle_open_time=300_000,
            candle_close_time=599_999,
            bullish_evidence=(),
            bearish_evidence=(
                evidence("macd", "BEARISH", "REGULAR", "110"),
                evidence("cmf", "BEARISH", "REGULAR", "110"),
            ),
        )
        strategy = self.strategy((bullish, bearish))
        strategy.set_position("FLAT")
        enter = strategy.on_candle(candle(0))
        self.assertIsNotNone(enter)
        assert enter is not None
        self.assertEqual((enter.action, enter.target_position), ("ENTER", "LONG"))
        self.assertEqual(enter.entry_signal.stop_price, Decimal("85.0"))
        self.assertIsNone(enter.entry_signal.take_profit_price)
        self.assertIsNone(
            StrategySignal.from_dict(enter.entry_signal.as_dict()).take_profit_price
        )
        strategy.set_position("LONG")
        exit_decision = strategy.on_candle(candle(1))
        self.assertIsNotNone(exit_decision)
        assert exit_decision is not None
        self.assertEqual((exit_decision.action, exit_decision.target_position), ("EXIT", "FLAT"))
        self.assertFalse(hasattr(exit_decision, "entry_signal"))
        strategy.set_position("FLAT")
        self.assertIsNone(strategy.on_candle(candle(2)))

    def test_reentry_watch_only_accepts_fully_qualified_opposite_direction(self):
        bullish = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("obv", "BULLISH", "REGULAR", "90"),
            ),
            bearish_evidence=(),
        )
        bearish_exit = DivergenceObservation(
            candle_open_time=300_000,
            candle_close_time=599_999,
            bullish_evidence=(),
            bearish_evidence=(
                evidence("macd", "BEARISH", "REGULAR", "110"),
                evidence("cmf", "BEARISH", "REGULAR", "110"),
            ),
        )
        wrong_direction = DivergenceObservation(
            candle_open_time=600_000,
            candle_close_time=899_999,
            bullish_evidence=(
                evidence(
                    "rsi", "BULLISH", "REGULAR", "90",
                    current_pivot_time=900_000, previous_pivot_time=600_000,
                ),
                evidence(
                    "obv", "BULLISH", "REGULAR", "90",
                    current_pivot_time=900_000, previous_pivot_time=600_000,
                ),
            ),
            bearish_evidence=(),
        )
        confirmed_short = DivergenceObservation(
            candle_open_time=900_000,
            candle_close_time=1_199_999,
            bullish_evidence=(),
            bearish_evidence=(
                evidence(
                    "macd", "BEARISH", "REGULAR", "110",
                    current_pivot_time=900_000, previous_pivot_time=600_000,
                ),
                evidence(
                    "cmf", "BEARISH", "REGULAR", "110",
                    current_pivot_time=900_000, previous_pivot_time=600_000,
                ),
            ),
        )
        strategy = self.strategy(
            (bullish, bearish_exit, wrong_direction, confirmed_short)
        )
        self.assertEqual(strategy.on_candle(candle(0)).action, "ENTER")
        strategy.set_position("LONG")
        self.assertEqual(strategy.on_candle(candle(1)).action, "EXIT")
        strategy.set_position("FLAT")
        self.assertIsNone(strategy.on_candle(candle(2)))
        reentry = strategy.on_candle(candle(3))
        self.assertIsNotNone(reentry)
        assert reentry is not None
        self.assertEqual((reentry.action, reentry.target_position), ("ENTER", "SHORT"))

    def test_two_slots_from_one_indicator_do_not_meet_threshold(self):
        observation = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("rsi", "BULLISH", "HIDDEN", "90"),
            ),
            bearish_evidence=(),
        )
        strategy = self.strategy((observation,))
        strategy.set_position("FLAT")
        self.assertIsNone(strategy.on_candle(candle(0)))

    def test_consumed_historical_pivot_cannot_trigger_a_second_trade(self):
        bullish = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("obv", "BULLISH", "REGULAR", "90"),
            ),
            bearish_evidence=(),
        )
        strategy = self.strategy((bullish, bullish))
        first = strategy.on_candle(candle(0))
        self.assertIsNotNone(first)
        strategy.set_position("FLAT")  # Simulate the first trade being stopped.
        self.assertIsNone(strategy.on_candle(candle(1)))

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


class VersionFourEntryFilterTests(unittest.TestCase):
    def strategy(self, observations, *, trend="LONG"):
        strategy = MultiDivergenceReversalStrategy(
            symbol="BTCUSDT",
            interval="5m",
            instance_id="v4-test",
            min_entry_divergences=3,
            min_reverse_divergences=2,
            min_entry_indicator_groups=2,
            trend_filter_enabled=True,
            structure_break_enabled=True,
            market_filter_enabled=False,
            position_management_enabled=False,
        )
        strategy._suite = _FakeSuite()  # type: ignore[attr-defined]
        strategy._detector = _FakeDetector(observations)  # type: ignore[attr-defined]
        strategy._trend = _FakeTrend(trend)  # type: ignore[attr-defined]
        return strategy

    @staticmethod
    def bullish_observation():
        return DivergenceObservation(
            candle_open_time=600_000,
            candle_close_time=899_999,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("macd", "BULLISH", "REGULAR", "90"),
                evidence("obv", "BULLISH", "REGULAR", "90"),
            ),
            bearish_evidence=(),
        )

    def test_entry_waits_for_structure_break_then_emits_stop_only_signal(self):
        observation = self.bullish_observation()
        strategy = self.strategy((None, None, observation, None))
        self.assertIsNone(strategy.on_candle(candle(0, high="105", low="95")))
        self.assertIsNone(strategy.on_candle(candle(1, high="104", low="94")))
        self.assertIsNone(strategy.on_candle(candle(2, high="103", low="93")))
        self.assertIsNotNone(strategy.pending_setup)
        entered = strategy.on_candle(
            candle(3, high="107", low="94", close="106")
        )
        self.assertIsNotNone(entered)
        assert entered is not None
        self.assertEqual((entered.action, entered.target_position), ("ENTER", "LONG"))
        self.assertEqual(entered.entry_signal.stop_price, Decimal("88.0"))
        self.assertIsNone(entered.entry_signal.take_profit_price)

    def test_three_correlated_oscillators_do_not_create_setup(self):
        observation = DivergenceObservation(
            candle_open_time=600_000,
            candle_close_time=899_999,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("stochastic", "BULLISH", "REGULAR", "90"),
                evidence("mfi", "BULLISH", "REGULAR", "90"),
            ),
            bearish_evidence=(),
        )
        strategy = self.strategy((None, None, observation))
        for index in range(3):
            self.assertIsNone(strategy.on_candle(candle(index)))
        self.assertIsNone(strategy.pending_setup)

    def test_invalidation_wins_over_breakout_and_same_pivot_is_not_rearmed(self):
        observation = self.bullish_observation()
        strategy = self.strategy((None, None, observation, None, observation))
        strategy.on_candle(candle(0, high="105", low="95"))
        strategy.on_candle(candle(1, high="104", low="94"))
        strategy.on_candle(candle(2, high="103", low="93"))
        self.assertIsNotNone(strategy.pending_setup)
        self.assertIsNone(
            strategy.on_candle(candle(3, high="107", low="90", close="106"))
        )
        self.assertIsNone(strategy.pending_setup)
        self.assertIsNone(strategy.on_candle(candle(4)))
        self.assertIsNone(strategy.pending_setup)

    def test_open_position_opposite_divergence_exits_without_reentry(self):
        bearish = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(),
            bearish_evidence=(
                evidence("rsi", "BEARISH", "REGULAR", "110"),
                evidence("cmf", "BEARISH", "REGULAR", "110"),
            ),
        )
        strategy = self.strategy((bearish,), trend="LONG")
        strategy.set_position("LONG")
        exit_decision = strategy.on_candle(candle(0))
        self.assertIsNotNone(exit_decision)
        assert exit_decision is not None
        self.assertEqual((exit_decision.action, exit_decision.target_position), ("EXIT", "FLAT"))
        self.assertIsNone(strategy.pending_setup)

    def test_exit_does_not_construct_a_low_quality_reverse_entry(self):
        bearish = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(),
            bearish_evidence=(
                evidence("rsi", "BEARISH", "REGULAR", "200"),
                evidence("cmf", "BEARISH", "REGULAR", "200"),
            ),
        )
        strategy = MultiDivergenceReversalStrategy(
            symbol="BTCUSDT",
            interval="5m",
            instance_id="v4-reverse-cost-test",
            min_stop_bps=Decimal("5000"),
            market_filter_enabled=False,
            position_management_enabled=False,
        )
        strategy._suite = _FakeSuite()  # type: ignore[attr-defined]
        strategy._detector = _FakeDetector((bearish,))  # type: ignore[attr-defined]
        strategy._trend = _FakeTrend("LONG")  # type: ignore[attr-defined]
        strategy.set_position("LONG")
        exit_decision = strategy.on_candle(candle(0))
        self.assertIsNotNone(exit_decision)
        assert exit_decision is not None
        self.assertEqual(exit_decision.action, "EXIT")
        self.assertFalse(hasattr(exit_decision, "entry_signal"))


class VersionFiveMarketAndManagementTests(unittest.TestCase):
    @staticmethod
    def observation(divergence_type="REGULAR"):
        return DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", divergence_type, "90"),
                evidence("cmf", "BULLISH", divergence_type, "90"),
            ),
            bearish_evidence=(),
        )

    def strategy(self, observations, **kwargs):
        strategy = MultiDivergenceReversalStrategy(
            symbol="BTCUSDT",
            interval="5m",
            instance_id="v5-test",
            min_entry_divergences=2,
            min_entry_indicator_groups=1,
            min_entry_group_score=Decimal("0.65"),
            require_non_oscillator=True,
            trend_filter_enabled=True,
            structure_break_enabled=False,
            min_stop_atr=Decimal("0.5"),
            **kwargs,
        )
        strategy._suite = _MetricSuite()  # type: ignore[attr-defined]
        strategy._detector = _FakeDetector(observations)  # type: ignore[attr-defined]
        strategy._trend = _FakeTrend("LONG")  # type: ignore[attr-defined]
        return strategy

    def test_regular_and_hidden_slots_do_not_combine(self):
        strategy = self.strategy((self.observation("REGULAR"),))
        decision = strategy.on_candle(candle(0))
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(dict(decision.entry_signal.indicators)["divergence_type"], "REGULAR")

        mixed = DivergenceObservation(
            candle_open_time=0,
            candle_close_time=1,
            bullish_evidence=(
                evidence("rsi", "BULLISH", "REGULAR", "90"),
                evidence("cmf", "BULLISH", "HIDDEN", "90"),
            ),
            bearish_evidence=(),
        )
        self.assertIsNone(self.strategy((mixed,)).on_candle(candle(0)))

    def test_market_filter_blocks_low_adx_and_volume(self):
        strategy = self.strategy((self.observation("HIDDEN"),))
        strategy._suite.last_adx = Decimal("10")  # type: ignore[attr-defined]
        self.assertIsNone(strategy.on_candle(candle(0)))
        strategy = self.strategy((self.observation("REGULAR"),))
        strategy._suite.last_volume_ratio = Decimal("0.5")  # type: ignore[attr-defined]
        self.assertIsNone(strategy.on_candle(candle(0)))

    def test_cost_filter_blocks_when_expected_atr_move_cannot_cover_cost(self):
        strategy = self.strategy((self.observation("REGULAR"),))
        self.assertIsNone(strategy.on_candle(candle(0, close="10000")))

    def test_management_emits_break_even_exit(self):
        strategy = self.strategy(
            (self.observation("REGULAR"), None),
            market_filter_enabled=False,
            break_even_trigger_r=Decimal("0.5"),
            trailing_start_r=Decimal("100"),
        )
        entry = strategy.on_candle(candle(0))
        self.assertIsNotNone(entry)
        strategy.set_position("LONG")
        self.assertIsNone(strategy.on_candle(candle(1, high="110", low="99", close="109")))
        exit_decision = strategy.on_candle(candle(2, high="109", low="98", close="99"))
        self.assertIsInstance(exit_decision, StrategyExitDecision)


class HourlyTrendAggregationTests(unittest.TestCase):
    def test_only_completed_aligned_hour_updates_trend(self):
        trend = _HourlyEmaTrend(2, 3)
        for index in range(36):
            hourly_close = "100" if index < 12 else "110" if index < 24 else "120"
            trend.update(candle(index, close=hourly_close))
        self.assertEqual(trend.completed_hours, 2)
        self.assertEqual(trend.state, "NEUTRAL")
        trend.update(candle(36, close="121"))
        self.assertEqual(trend.completed_hours, 3)
        self.assertEqual(trend.state, "LONG")


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
