from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from decimal import Decimal

from ..candles import Candle
from .base import (
    DivergenceEvidence,
    StrategyDecision,
    StrategyExitDecision,
    StrategySignal,
)
from .divergence import ConfirmedDivergenceDetector, DivergenceObservation
from .indicators import (
    ChaikinMoneyFlow,
    CommodityChannelIndex,
    ExponentialMovingAverage,
    MoneyFlowIndex,
    Momentum,
    MovingAverageConvergenceDivergence,
    OnBalanceVolume,
    RelativeStrengthIndex,
    StochasticOscillator,
    SimpleMovingAverage,
    VolumeWeightedMacd,
    WilderAverageTrueRange,
    WilderDirectionalMovementIndex,
)


DEFAULT_INDICATORS = (
    "macd",
    "macd_histogram",
    "rsi",
    "stochastic",
    "cci",
    "momentum",
    "obv",
    "vwmacd",
    "cmf",
    "mfi",
)

INDICATOR_GROUPS = {
    "rsi": "OSCILLATOR",
    "stochastic": "OSCILLATOR",
    "cci": "OSCILLATOR",
    "momentum": "OSCILLATOR",
    "mfi": "OSCILLATOR",
    "macd": "TREND_MOMENTUM",
    "macd_histogram": "TREND_MOMENTUM",
    "vwmacd": "TREND_MOMENTUM",
    "obv": "VOLUME",
    "cmf": "VOLUME",
}


class _IndicatorSuite:
    def __init__(self, atr_period: int, adx_period: int, volume_period: int) -> None:
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.volume_period = volume_period
        self.reset()

    def reset(self) -> None:
        self.rsi = RelativeStrengthIndex(14)
        self.macd = MovingAverageConvergenceDivergence(12, 26, 9)
        self.stochastic = StochasticOscillator(14, 3)
        self.cci = CommodityChannelIndex(10)
        self.momentum = Momentum(10)
        self.obv = OnBalanceVolume()
        self.vwmacd = VolumeWeightedMacd(12, 26)
        self.cmf = ChaikinMoneyFlow(21)
        self.mfi = MoneyFlowIndex(14)
        self.atr = WilderAverageTrueRange(self.atr_period)
        self.adx = WilderDirectionalMovementIndex(self.adx_period)
        self.volume_average = SimpleMovingAverage(self.volume_period)
        self.last_adx: Decimal | None = None
        self.last_volume_ratio: Decimal | None = None
        self.last_atr_bps: Decimal | None = None

    def update(
        self, candle: Candle
    ) -> tuple[dict[str, Decimal | None], Decimal | None]:
        high = Decimal(candle.high)
        low = Decimal(candle.low)
        close = Decimal(candle.close)
        volume = Decimal(candle.volume)
        macd, _, histogram = self.macd.update(close)
        values = {
            "rsi": self.rsi.update(close),
            "macd": macd,
            "macd_histogram": histogram,
            "stochastic": self.stochastic.update(high, low, close),
            "cci": self.cci.update(high, low, close),
            "momentum": self.momentum.update(close),
            "obv": self.obv.update(close, volume),
            "vwmacd": self.vwmacd.update(close, volume),
            "cmf": self.cmf.update(high, low, close, volume),
            "mfi": self.mfi.update(close, volume),
        }
        atr = self.atr.update(high, low, close)
        self.last_adx, _, _ = self.adx.update(high, low, close)
        average_volume = self.volume_average.update(volume)
        self.last_volume_ratio = (
            volume / average_volume
            if average_volume not in {None, Decimal(0)}
            else None
        )
        self.last_atr_bps = (
            atr / close * Decimal("10000")
            if atr is not None and close > 0
            else None
        )
        return values, atr


class _HourlyEmaTrend:
    """Aggregate closed 5m candles without exposing an unfinished 1h bar."""

    _FIVE_MINUTES_MS = 300_000
    _ONE_HOUR_MS = 3_600_000

    def __init__(self, fast_period: int, slow_period: int) -> None:
        if fast_period < 1 or fast_period >= slow_period:
            raise ValueError("hourly trend EMA periods must satisfy 1 <= fast < slow")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.reset()

    def reset(self) -> None:
        self._fast = ExponentialMovingAverage(self.fast_period)
        self._slow = ExponentialMovingAverage(self.slow_period)
        self._bucket_start: int | None = None
        self._bucket: list[Candle] = []
        self.last_completed_close: Decimal | None = None
        self.fast_value: Decimal | None = None
        self.slow_value: Decimal | None = None
        self.state = "NEUTRAL"
        self.completed_hours = 0

    def update(self, candle: Candle) -> str:
        bucket_start = candle.open_time - candle.open_time % self._ONE_HOUR_MS
        if self._bucket_start is None:
            self._bucket_start = bucket_start
        elif bucket_start != self._bucket_start:
            self._finalize_bucket()
            self._bucket_start = bucket_start
            self._bucket = []
        self._bucket.append(candle)
        return self.state

    def _finalize_bucket(self) -> None:
        if self._bucket_start is None or len(self._bucket) != 12:
            return
        expected = [
            self._bucket_start + index * self._FIVE_MINUTES_MS
            for index in range(12)
        ]
        if [candle.open_time for candle in self._bucket] != expected:
            return
        close = Decimal(self._bucket[-1].close)
        self.last_completed_close = close
        self.fast_value = self._fast.update(close)
        self.slow_value = self._slow.update(close)
        self.completed_hours += 1
        if self.fast_value is None or self.slow_value is None:
            self.state = "NEUTRAL"
        elif close > self.slow_value and self.fast_value > self.slow_value:
            self.state = "LONG"
        elif close < self.slow_value and self.fast_value < self.slow_value:
            self.state = "SHORT"
        else:
            self.state = "NEUTRAL"


@dataclass(slots=True)
class _PendingDivergenceSetup:
    target: str
    direction: str
    created_index: int
    evidence: tuple[DivergenceEvidence, ...]
    pivot_keys: tuple[tuple[str, int], ...]
    indicator_count: int
    group_count: int
    group_score: Decimal
    divergence_type: str
    trigger_price: Decimal
    invalidation_price: Decimal
    extreme_price: Decimal


class MultiDivergenceReversalStrategy:
    name = "multi-divergence-reversal-v1"
    version = "5"

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        instance_id: str | None = None,
        pivot_period: int = 5,
        pivot_source: str = "high_low",
        max_pivots: int = 10,
        max_bars_to_check: int = 100,
        divergence_types: tuple[str, ...] = ("regular", "hidden"),
        indicators: tuple[str, ...] = DEFAULT_INDICATORS,
        min_entry_divergences: int = 3,
        min_reverse_divergences: int = 2,
        min_entry_indicator_groups: int = 2,
        min_entry_group_score: Decimal = Decimal("0.65"),
        min_exit_group_score: Decimal = Decimal("0.65"),
        oscillator_group_weight: Decimal = Decimal("0.35"),
        trend_group_weight: Decimal = Decimal("0.35"),
        volume_group_weight: Decimal = Decimal("0.30"),
        require_non_oscillator: bool = True,
        pine_show_limit: int = 1,
        require_confirmation: bool = True,
        trend_filter_enabled: bool = True,
        trend_fast_ema: int = 50,
        trend_slow_ema: int = 200,
        structure_break_enabled: bool = True,
        breakout_lookback_bars: int = 3,
        setup_expiry_bars: int = 6,
        setup_invalidation_atr: Decimal = Decimal("0.25"),
        atr_period: int = 14,
        adx_period: int = 14,
        hidden_min_adx: Decimal = Decimal("18"),
        regular_max_adx: Decimal = Decimal("30"),
        volume_average_period: int = 20,
        min_volume_ratio: Decimal = Decimal("0.8"),
        estimated_cost_bps_per_side: Decimal = Decimal("15"),
        expected_move_atr: Decimal = Decimal("2"),
        min_reward_cost_ratio: Decimal = Decimal("1.5"),
        stop_atr_buffer: Decimal = Decimal("0.5"),
        min_stop_atr: Decimal = Decimal("0.75"),
        max_stop_atr: Decimal = Decimal("3"),
        min_stop_bps: Decimal = Decimal("0"),
        risk_usdt: Decimal = Decimal("1"),
        leverage: int = 3,
        margin_utilization: Decimal = Decimal("0.50"),
        break_even_trigger_r: Decimal = Decimal("1"),
        trailing_start_r: Decimal = Decimal("1.5"),
        trailing_atr_multiple: Decimal = Decimal("1"),
        max_hold_bars: int = 24,
        min_progress_r: Decimal = Decimal("0.5"),
        reentry_expiry_bars: int = 6,
        market_filter_enabled: bool = True,
        position_management_enabled: bool = True,
    ) -> None:
        if interval != "5m":
            raise ValueError("multi-divergence-reversal-v1 requires 5m candles")
        unknown = set(indicators) - set(DEFAULT_INDICATORS)
        if unknown or not indicators:
            raise ValueError(f"unknown or empty divergence indicators: {sorted(unknown)}")
        if min_entry_divergences < 2 or min_reverse_divergences < 2:
            raise ValueError("entry and reverse divergence thresholds must be at least 2")
        if (
            min_entry_divergences > len(indicators)
            or min_reverse_divergences > len(indicators)
        ):
            raise ValueError("divergence threshold exceeds enabled indicator count")
        enabled_groups = {INDICATOR_GROUPS[indicator] for indicator in indicators}
        if not 1 <= min_entry_indicator_groups <= len(enabled_groups):
            raise ValueError("entry indicator group threshold is invalid")
        if (
            not 1 <= breakout_lookback_bars <= 20
            or setup_expiry_bars < 1
            or max_hold_bars < 1
            or reentry_expiry_bars < 1
            or adx_period < 1
            or volume_average_period < 1
        ):
            raise ValueError("structure breakout parameters are invalid")
        values = (
            Decimal(setup_invalidation_atr),
            Decimal(stop_atr_buffer),
            Decimal(min_stop_atr),
            Decimal(max_stop_atr),
            Decimal(min_stop_bps),
            Decimal(risk_usdt),
            Decimal(margin_utilization),
        )
        if (
            any(value <= 0 for value in values[:4])
            or values[4] < 0
            or values[5] <= 0
            or not Decimal("0") < values[-1] <= Decimal("1")
        ):
            raise ValueError("divergence risk, setup and ATR parameters are invalid")
        if Decimal(min_stop_atr) > Decimal(max_stop_atr):
            raise ValueError("minimum stop ATR cannot exceed maximum stop ATR")
        if leverage < 1:
            raise ValueError("strategy leverage must be positive")
        group_weights = {
            "OSCILLATOR": Decimal(oscillator_group_weight),
            "TREND_MOMENTUM": Decimal(trend_group_weight),
            "VOLUME": Decimal(volume_group_weight),
        }
        if any(value <= 0 for value in group_weights.values()):
            raise ValueError("indicator group weights must be positive")
        if sum(group_weights.values(), Decimal(0)) != Decimal(1):
            raise ValueError("indicator group weights must sum to 1")
        thresholds = (
            Decimal(min_entry_group_score),
            Decimal(min_exit_group_score),
        )
        if any(not Decimal(0) < value <= Decimal(1) for value in thresholds):
            raise ValueError("group score thresholds must be in (0, 1]")
        market_values = (
            Decimal(hidden_min_adx),
            Decimal(regular_max_adx),
            Decimal(min_volume_ratio),
            Decimal(estimated_cost_bps_per_side),
            Decimal(expected_move_atr),
            Decimal(min_reward_cost_ratio),
        )
        if any(value < 0 for value in market_values[:4]) or any(
            value <= 0 for value in market_values[4:]
        ):
            raise ValueError("market and cost filters are invalid")
        management_values = (
            Decimal(break_even_trigger_r),
            Decimal(trailing_start_r),
            Decimal(trailing_atr_multiple),
            Decimal(min_progress_r),
        )
        if any(value <= 0 for value in management_values):
            raise ValueError("position management parameters must be positive")
        self.symbol = symbol.upper()
        self.interval = interval
        self.instance_id = instance_id or self.name
        self.indicators = tuple(indicators)
        self.pivot_period = pivot_period
        self.pivot_source = pivot_source
        self.max_pivots = max_pivots
        self.max_bars_to_check = max_bars_to_check
        self.divergence_types = tuple(divergence_types)
        self.min_entry_divergences = min_entry_divergences
        self.min_reverse_divergences = min_reverse_divergences
        self.min_entry_indicator_groups = min_entry_indicator_groups
        self.min_entry_group_score = Decimal(min_entry_group_score)
        self.min_exit_group_score = Decimal(min_exit_group_score)
        self.group_weights = group_weights
        self.require_non_oscillator = require_non_oscillator
        self.pine_show_limit = pine_show_limit
        self.require_confirmation = require_confirmation
        self.trend_filter_enabled = trend_filter_enabled
        self.trend_fast_ema = trend_fast_ema
        self.trend_slow_ema = trend_slow_ema
        self.structure_break_enabled = structure_break_enabled
        self.breakout_lookback_bars = breakout_lookback_bars
        self.setup_expiry_bars = setup_expiry_bars
        self.setup_invalidation_atr = Decimal(setup_invalidation_atr)
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.hidden_min_adx = Decimal(hidden_min_adx)
        self.regular_max_adx = Decimal(regular_max_adx)
        self.volume_average_period = volume_average_period
        self.min_volume_ratio = Decimal(min_volume_ratio)
        self.estimated_cost_bps_per_side = Decimal(estimated_cost_bps_per_side)
        self.expected_move_atr = Decimal(expected_move_atr)
        self.min_reward_cost_ratio = Decimal(min_reward_cost_ratio)
        self.stop_atr_buffer = Decimal(stop_atr_buffer)
        self.min_stop_atr = Decimal(min_stop_atr)
        self.max_stop_atr = Decimal(max_stop_atr)
        self.min_stop_bps = Decimal(min_stop_bps)
        self.risk_usdt = Decimal(risk_usdt)
        self.leverage = leverage
        self.margin_utilization = Decimal(margin_utilization)
        self.break_even_trigger_r = Decimal(break_even_trigger_r)
        self.trailing_start_r = Decimal(trailing_start_r)
        self.trailing_atr_multiple = Decimal(trailing_atr_multiple)
        self.max_hold_bars = max_hold_bars
        self.min_progress_r = Decimal(min_progress_r)
        self.reentry_expiry_bars = reentry_expiry_bars
        self.market_filter_enabled = market_filter_enabled
        self.position_management_enabled = position_management_enabled
        self.cooldown_bars = 0
        self.reset()

    def reset(self) -> None:
        self._suite = _IndicatorSuite(
            self.atr_period, self.adx_period, self.volume_average_period
        )
        self._detector = ConfirmedDivergenceDetector(
            indicator_names=self.indicators,
            pivot_period=self.pivot_period,
            pivot_source=self.pivot_source,
            max_pivots=self.max_pivots,
            max_bars_to_check=self.max_bars_to_check,
            divergence_types=self.divergence_types,
            minimum_divergences=self.pine_show_limit,
            require_confirmation=self.require_confirmation,
        )
        self._trend = _HourlyEmaTrend(self.trend_fast_ema, self.trend_slow_ema)
        self._recent_candles: deque[Candle] = deque(maxlen=self.breakout_lookback_bars)
        self._position = "FLAT"
        self._last_open_time: int | None = None
        self._bar_index = -1
        self._emitted: set[str] = set()
        self._consumed_pivots: set[tuple[str, int]] = set()
        self._seen_setup_pivots: set[tuple[str, int]] = set()
        self._pending_setup: _PendingDivergenceSetup | None = None
        self._pending_fill_signal: StrategySignal | None = None
        self._position_entry_reference: Decimal | None = None
        self._position_initial_stop: Decimal | None = None
        self._position_started_index: int | None = None
        self._position_best_price: Decimal | None = None
        self._break_even_armed = False
        self._reentry_target: str | None = None
        self._reentry_expires_index: int | None = None
        self.last_observation: DivergenceObservation | None = None

    @property
    def pending_setup(self) -> _PendingDivergenceSetup | None:
        return self._pending_setup

    @property
    def hourly_trend(self) -> str:
        return self._trend.state

    def set_position(self, position: str) -> None:
        if position not in {"FLAT", "LONG", "SHORT"}:
            raise ValueError("strategy position must be FLAT, LONG or SHORT")
        previous = self._position
        self._position = position
        if position != "FLAT":
            self._pending_setup = None
            if previous == "FLAT" and self._pending_fill_signal is not None:
                expected = "LONG" if self._pending_fill_signal.side == "BUY" else "SHORT"
                if expected == position:
                    self._position_entry_reference = self._pending_fill_signal.reference_price
                    self._position_initial_stop = self._pending_fill_signal.stop_price
                    self._position_started_index = self._bar_index + 1
                    self._position_best_price = self._pending_fill_signal.reference_price
                    self._break_even_armed = False
                    self._reentry_target = None
                    self._reentry_expires_index = None
            self._pending_fill_signal = None
        elif previous != "FLAT":
            self._position_entry_reference = None
            self._position_initial_stop = None
            self._position_started_index = None
            self._position_best_price = None
            self._break_even_armed = False

    def on_candle(
        self, candle: Candle
    ) -> StrategyDecision | StrategyExitDecision | None:
        self._validate_candle(candle)
        self._bar_index += 1
        self._recent_candles.append(candle)
        indicator_values, atr = self._suite.update(candle)
        self._trend.update(candle)
        raw_observation = self._detector.update(candle, indicator_values)
        observation = self._without_consumed_pivots(raw_observation)
        self.last_observation = observation

        if self._position != "FLAT":
            self._pending_setup = None
            managed = self._position_management_decision(candle, atr)
            if managed is not None:
                return managed
            return self._exit_decision(candle, observation)

        if (
            self._reentry_expires_index is not None
            and self._bar_index > self._reentry_expires_index
        ):
            self._reentry_target = None
            self._reentry_expires_index = None

        pending_decision = self._advance_pending_setup(candle, atr)
        if pending_decision is not None:
            return pending_decision
        if observation is None or atr is None:
            return None
        return self._create_entry_setup_or_decision(candle, observation, atr)

    def _validate_candle(self, candle: Candle) -> None:
        if not candle.closed:
            raise ValueError("strategy accepts closed candles only")
        if candle.symbol.upper() != self.symbol or candle.interval != self.interval:
            raise ValueError("candle does not match strategy symbol and interval")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError("strategy candles must be strictly increasing")
        self._last_open_time = candle.open_time

    def _without_consumed_pivots(
        self, observation: DivergenceObservation | None
    ) -> DivergenceObservation | None:
        if observation is None:
            return None
        filtered = DivergenceObservation(
            candle_open_time=observation.candle_open_time,
            candle_close_time=observation.candle_close_time,
            bullish_evidence=tuple(
                item
                for item in observation.bullish_evidence
                if self._pivot_key(item) not in self._consumed_pivots
            ),
            bearish_evidence=tuple(
                item
                for item in observation.bearish_evidence
                if self._pivot_key(item) not in self._consumed_pivots
            ),
        )
        if not filtered.bullish_evidence and not filtered.bearish_evidence:
            return None
        return filtered

    def _exit_decision(
        self,
        candle: Candle,
        observation: DivergenceObservation | None,
    ) -> StrategyExitDecision | None:
        if observation is None:
            return None
        bullish = self._select_exit_evidence(observation.bullish_evidence)
        bearish = self._select_exit_evidence(observation.bearish_evidence)
        if bullish is not None and bearish is not None:
            return None
        if self._position == "LONG" and bearish is not None:
            return self._emit_exit(
                candle,
                evidence=bearish,
                reentry_target="SHORT",
                reason="opposite bearish divergence: close long and await confirmed short",
            )
        if self._position == "SHORT" and bullish is not None:
            return self._emit_exit(
                candle,
                evidence=bullish,
                reentry_target="LONG",
                reason="opposite bullish divergence: close short and await confirmed long",
            )
        return None

    def _position_management_decision(
        self, candle: Candle, atr: Decimal | None
    ) -> StrategyExitDecision | None:
        if (
            not self.position_management_enabled
            or atr is None
            or self._position_entry_reference is None
            or self._position_initial_stop is None
            or self._position_started_index is None
        ):
            return None
        entry = self._position_entry_reference
        initial_risk = abs(entry - self._position_initial_stop)
        if initial_risk <= 0:
            return None
        high = Decimal(candle.high)
        low = Decimal(candle.low)
        close = Decimal(candle.close)
        if self._position == "LONG":
            self._position_best_price = max(self._position_best_price or entry, high)
            favorable_r = (self._position_best_price - entry) / initial_risk
            break_even_hit = self._break_even_armed and close <= entry
            trail_hit = (
                favorable_r >= self.trailing_start_r
                and close
                <= self._position_best_price - atr * self.trailing_atr_multiple
            )
        else:
            self._position_best_price = min(self._position_best_price or entry, low)
            favorable_r = (entry - self._position_best_price) / initial_risk
            break_even_hit = self._break_even_armed and close >= entry
            trail_hit = (
                favorable_r >= self.trailing_start_r
                and close
                >= self._position_best_price + atr * self.trailing_atr_multiple
            )
        if favorable_r >= self.break_even_trigger_r:
            self._break_even_armed = True
        if trail_hit:
            return self._emit_exit(candle, evidence=(), reason="ATR trailing exit")
        if break_even_hit:
            return self._emit_exit(candle, evidence=(), reason="break-even protection exit")
        held_bars = self._bar_index - self._position_started_index + 1
        if held_bars >= self.max_hold_bars and favorable_r < self.min_progress_r:
            return self._emit_exit(
                candle,
                evidence=(),
                reason="time exit after insufficient favorable progress",
            )
        return None

    def _emit_exit(
        self,
        candle: Candle,
        *,
        evidence: tuple[DivergenceEvidence, ...],
        reason: str,
        reentry_target: str | None = None,
    ) -> StrategyExitDecision | None:
        decision = StrategyExitDecision(
            strategy=self.name,
            version=self.version,
            instance_id=self.instance_id,
            symbol=self.symbol,
            interval=self.interval,
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            current_position=self._position,
            bullish_count=self._unique_indicator_count(
                tuple(item for item in evidence if item.direction == "BULLISH")
            ),
            bearish_count=self._unique_indicator_count(
                tuple(item for item in evidence if item.direction == "BEARISH")
            ),
            evidence=evidence,
            reason=reason,
        )
        if decision.decision_id in self._emitted:
            return None
        self._emitted.add(decision.decision_id)
        self._consumed_pivots.update(self._pivot_key(item) for item in evidence)
        self._reentry_target = reentry_target
        self._reentry_expires_index = (
            self._bar_index + self.reentry_expiry_bars
            if reentry_target is not None
            else None
        )
        return decision

    def _advance_pending_setup(
        self, candle: Candle, atr: Decimal | None
    ) -> StrategyDecision | None:
        setup = self._pending_setup
        if setup is None:
            return None
        age = self._bar_index - setup.created_index
        if age > self.setup_expiry_bars:
            self._pending_setup = None
            return None
        if not self._market_allows(
            setup.target, setup.divergence_type, candle, atr
        ):
            self._pending_setup = None
            return None
        high = Decimal(candle.high)
        low = Decimal(candle.low)
        close = Decimal(candle.close)
        invalidated = (
            low <= setup.invalidation_price
            if setup.target == "LONG"
            else high >= setup.invalidation_price
        )
        if invalidated:
            self._pending_setup = None
            return None
        if setup.target == "LONG":
            setup.extreme_price = min(setup.extreme_price, low)
            triggered = close > setup.trigger_price
            stop = setup.extreme_price - (atr or Decimal(0)) * self.stop_atr_buffer
        else:
            setup.extreme_price = max(setup.extreme_price, high)
            triggered = close < setup.trigger_price
            stop = setup.extreme_price + (atr or Decimal(0)) * self.stop_atr_buffer
        if not triggered or atr is None:
            return None
        self._pending_setup = None
        decision = self._emit_decision(
            candle,
            action="ENTER",
            target=setup.target,
            signal_evidence=setup.evidence,
            decision_evidence=setup.evidence,
            atr=atr,
            bullish_count=(setup.indicator_count if setup.target == "LONG" else 0),
            bearish_count=(setup.indicator_count if setup.target == "SHORT" else 0),
            stop_override=stop,
            reason=(
                f"{setup.divergence_type.lower()} {setup.target.lower()} divergence "
                f"setup confirmed by structure break"
            ),
        )
        return decision

    def _create_entry_setup_or_decision(
        self,
        candle: Candle,
        observation: DivergenceObservation,
        atr: Decimal,
    ) -> StrategyDecision | None:
        bullish_evidence = tuple(
            item
            for item in observation.bullish_evidence
            if self._pivot_key(item) not in self._seen_setup_pivots
        )
        bearish_evidence = tuple(
            item
            for item in observation.bearish_evidence
            if self._pivot_key(item) not in self._seen_setup_pivots
        )
        bullish = self._select_entry_evidence(
            bullish_evidence, "LONG", candle, atr
        )
        bearish = self._select_entry_evidence(
            bearish_evidence, "SHORT", candle, atr
        )
        if bullish is not None and bearish is not None:
            self._pending_setup = None
            return None
        if self._pending_setup is not None:
            return None
        if bullish is not None:
            evidence, divergence_type, indicator_count, group_count, group_score = bullish
            return self._start_entry(
                candle,
                "LONG",
                evidence,
                divergence_type,
                indicator_count,
                group_count,
                group_score,
                atr,
            )
        if bearish is not None:
            evidence, divergence_type, indicator_count, group_count, group_score = bearish
            return self._start_entry(
                candle,
                "SHORT",
                evidence,
                divergence_type,
                indicator_count,
                group_count,
                group_score,
                atr,
            )
        return None

    def _select_entry_evidence(
        self,
        evidence: tuple[DivergenceEvidence, ...],
        target: str,
        candle: Candle,
        atr: Decimal,
    ) -> tuple[tuple[DivergenceEvidence, ...], str, int, int, Decimal] | None:
        if self._reentry_target is not None and target != self._reentry_target:
            return None
        candidates = []
        for divergence_type in ("REGULAR", "HIDDEN"):
            typed = tuple(
                item for item in evidence if item.divergence_type == divergence_type
            )
            indicator_count = self._unique_indicator_count(typed)
            group_count = self._indicator_group_count(typed)
            group_score = self._indicator_group_score(typed)
            has_non_oscillator = any(
                INDICATOR_GROUPS[item.indicator] != "OSCILLATOR" for item in typed
            )
            if (
                indicator_count >= self.min_entry_divergences
                and group_count >= self.min_entry_indicator_groups
                and group_score >= self.min_entry_group_score
                and (has_non_oscillator or not self.require_non_oscillator)
                and self._market_allows(target, divergence_type, candle, atr)
            ):
                candidates.append(
                    (typed, divergence_type, indicator_count, group_count, group_score)
                )
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[4], item[2]))

    def _select_exit_evidence(
        self, evidence: tuple[DivergenceEvidence, ...]
    ) -> tuple[DivergenceEvidence, ...] | None:
        candidates: list[tuple[tuple[DivergenceEvidence, ...], Decimal, int]] = []
        for divergence_type in ("REGULAR", "HIDDEN"):
            typed = tuple(
                item for item in evidence if item.divergence_type == divergence_type
            )
            score = self._indicator_group_score(typed)
            count = self._unique_indicator_count(typed)
            has_non_oscillator = any(
                INDICATOR_GROUPS[item.indicator] != "OSCILLATOR" for item in typed
            )
            if (
                count >= self.min_reverse_divergences
                and score >= self.min_exit_group_score
                and (has_non_oscillator or not self.require_non_oscillator)
            ):
                candidates.append((typed, score, count))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[1], item[2]))[0]

    def _start_entry(
        self,
        candle: Candle,
        target: str,
        evidence: tuple[DivergenceEvidence, ...],
        divergence_type: str,
        indicator_count: int,
        group_count: int,
        group_score: Decimal,
        atr: Decimal,
    ) -> StrategyDecision | None:
        pivot_keys = tuple(sorted({self._pivot_key(item) for item in evidence}))
        self._seen_setup_pivots.update(pivot_keys)
        if not self.structure_break_enabled:
            return self._emit_decision(
                candle,
                action="ENTER",
                target=target,
                signal_evidence=evidence,
                decision_evidence=evidence,
                atr=atr,
                bullish_count=(indicator_count if target == "LONG" else 0),
                bearish_count=(indicator_count if target == "SHORT" else 0),
                reason=(
                    f"{divergence_type.lower()} {target.lower()} divergence entry "
                    "without structure filter"
                ),
            )
        if len(self._recent_candles) < self.breakout_lookback_bars:
            return None
        highs = [Decimal(item.high) for item in self._recent_candles]
        lows = [Decimal(item.low) for item in self._recent_candles]
        if target == "LONG":
            trigger = max(highs)
            extreme = min(lows)
            invalidation = extreme - atr * self.setup_invalidation_atr
            direction = "BULLISH"
        else:
            trigger = min(lows)
            extreme = max(highs)
            invalidation = extreme + atr * self.setup_invalidation_atr
            direction = "BEARISH"
        self._pending_setup = _PendingDivergenceSetup(
            target=target,
            direction=direction,
            created_index=self._bar_index,
            evidence=evidence,
            pivot_keys=pivot_keys,
            indicator_count=indicator_count,
            group_count=group_count,
            group_score=group_score,
            divergence_type=divergence_type,
            trigger_price=trigger,
            invalidation_price=invalidation,
            extreme_price=extreme,
        )
        return None

    def _emit_decision(
        self,
        candle: Candle,
        *,
        action: str,
        target: str,
        signal_evidence: tuple[DivergenceEvidence, ...],
        decision_evidence: tuple[DivergenceEvidence, ...],
        atr: Decimal,
        bullish_count: int,
        bearish_count: int,
        reason: str,
        stop_override: Decimal | None = None,
    ) -> StrategyDecision | None:
        signal = self._entry_signal(
            candle,
            target,
            signal_evidence,
            bullish_count,
            bearish_count,
            atr,
            stop_override=stop_override,
            enforce_entry_filters=(action == "ENTER"),
        )
        if signal is None:
            return None
        decision = StrategyDecision(
            strategy=self.name,
            version=self.version,
            instance_id=self.instance_id,
            symbol=self.symbol,
            interval=self.interval,
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            action=action,
            current_position=self._position,
            target_position=target,
            bullish_count=bullish_count,
            bearish_count=bearish_count,
            evidence=decision_evidence,
            entry_signal=signal,
            reason=reason,
        )
        if decision.decision_id in self._emitted:
            return None
        self._emitted.add(decision.decision_id)
        self._consumed_pivots.update(
            self._pivot_key(item) for item in signal_evidence
        )
        self._pending_fill_signal = signal
        return decision

    def _entry_signal(
        self,
        candle: Candle,
        target: str,
        evidence: tuple[DivergenceEvidence, ...],
        bullish_count: int,
        bearish_count: int,
        atr: Decimal,
        *,
        stop_override: Decimal | None = None,
        enforce_entry_filters: bool = True,
    ) -> StrategySignal | None:
        reference = Decimal(candle.close)
        if stop_override is not None:
            stop = stop_override
            side = "BUY" if target == "LONG" else "SELL"
            distance = reference - stop if target == "LONG" else stop - reference
        elif target == "LONG":
            anchor = min(item.current_price for item in evidence)
            stop = anchor - atr * self.stop_atr_buffer
            distance = reference - stop
            side = "BUY"
        else:
            anchor = max(item.current_price for item in evidence)
            stop = anchor + atr * self.stop_atr_buffer
            distance = stop - reference
            side = "SELL"
        if stop <= 0 or distance <= 0:
            return None
        if enforce_entry_filters:
            if distance < atr * self.min_stop_atr or distance > atr * self.max_stop_atr:
                return None
        else:
            distance = min(
                max(distance, atr * self.min_stop_atr), atr * self.max_stop_atr
            )
            stop = reference - distance if target == "LONG" else reference + distance
            if stop <= 0:
                return None
        distance_bps = distance / reference * Decimal("10000")
        if enforce_entry_filters and distance_bps < self.min_stop_bps:
            return None
        evidence_json = json.dumps(
            [item.as_dict() for item in evidence], sort_keys=True, separators=(",", ":")
        )
        divergence_type = (
            next(iter({item.divergence_type for item in evidence}), "UNKNOWN")
        )
        group_score = self._indicator_group_score(evidence)
        adx = getattr(self._suite, "last_adx", None)
        volume_ratio = getattr(self._suite, "last_volume_ratio", None)
        atr_bps = atr / reference * Decimal("10000")
        round_trip_cost = self.estimated_cost_bps_per_side * Decimal(2)
        reward_cost_ratio = (
            atr_bps * self.expected_move_atr / round_trip_cost
            if round_trip_cost > 0
            else Decimal("Infinity")
        )
        return StrategySignal(
            strategy=self.name,
            version=self.version,
            symbol=self.symbol,
            interval=self.interval,
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            side=side,
            reference_price=reference,
            stop_price=stop,
            take_profit_price=None,
            risk_usdt=self.risk_usdt,
            leverage=self.leverage,
            margin_utilization=self.margin_utilization,
            indicators=(
                ("bullish_count", str(bullish_count)),
                ("bearish_count", str(bearish_count)),
                ("atr", str(atr)),
                ("stop_distance_bps", str(distance_bps)),
                (
                    "min_stop_bps",
                    str(self.min_stop_bps if enforce_entry_filters else Decimal(0)),
                ),
                ("pivot_period", str(self.pivot_period)),
                ("pivot_source", self.pivot_source),
                ("pine_show_limit", str(self.pine_show_limit)),
                ("count_mode", "unique_indicators"),
                ("divergence_type", divergence_type),
                ("indicator_group_score", str(group_score)),
                ("adx", str(adx) if adx is not None else "NA"),
                (
                    "volume_ratio",
                    str(volume_ratio) if volume_ratio is not None else "NA",
                ),
                ("atr_bps", str(atr_bps)),
                ("expected_reward_cost_ratio", str(reward_cost_ratio)),
                ("hourly_trend", self._trend.state),
                (
                    "exit_policy",
                    "opposite_divergence_or_managed_exit_or_stop",
                ),
                ("evidence", evidence_json),
            ),
            reason=(
                f"confirmed {target.lower()} divergence with "
                f"{len({item.indicator for item in evidence})} different indicators"
            ),
            instance_id=self.instance_id,
        )

    def _trend_allows(self, target: str) -> bool:
        if not self.trend_filter_enabled:
            return True
        return self._trend.state == target

    def _market_allows(
        self,
        target: str,
        divergence_type: str,
        candle: Candle,
        atr: Decimal | None,
    ) -> bool:
        if not self.market_filter_enabled:
            return True
        if atr is None:
            return False
        adx = getattr(self._suite, "last_adx", None)
        volume_ratio = getattr(self._suite, "last_volume_ratio", None)
        close = Decimal(candle.close)
        if adx is None or volume_ratio is None or close <= 0:
            return False
        if volume_ratio < self.min_volume_ratio:
            return False
        atr_bps = atr / close * Decimal("10000")
        round_trip_cost = self.estimated_cost_bps_per_side * Decimal(2)
        if (
            round_trip_cost > 0
            and atr_bps * self.expected_move_atr
            < round_trip_cost * self.min_reward_cost_ratio
        ):
            return False
        if divergence_type == "HIDDEN":
            return self._trend_allows(target) and adx >= self.hidden_min_adx
        if divergence_type != "REGULAR":
            return False
        if not self.trend_filter_enabled or self._trend.state in {"NEUTRAL", target}:
            return adx <= self.regular_max_adx or self._trend.state == target
        return adx <= self.regular_max_adx

    @staticmethod
    def _unique_indicator_count(evidence: tuple[DivergenceEvidence, ...]) -> int:
        return len({item.indicator for item in evidence})

    @staticmethod
    def _indicator_group_count(evidence: tuple[DivergenceEvidence, ...]) -> int:
        return len({INDICATOR_GROUPS[item.indicator] for item in evidence})

    def _indicator_group_score(
        self, evidence: tuple[DivergenceEvidence, ...]
    ) -> Decimal:
        groups = {INDICATOR_GROUPS[item.indicator] for item in evidence}
        return sum((self.group_weights[group] for group in groups), Decimal(0))

    @staticmethod
    def _pivot_key(item: DivergenceEvidence) -> tuple[str, int]:
        return item.direction, item.previous_pivot_time
