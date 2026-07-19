from __future__ import annotations

import json
from decimal import Decimal

from ..candles import Candle
from .base import StrategyDecision, StrategySignal
from .divergence import ConfirmedDivergenceDetector, DivergenceObservation
from .indicators import (
    ChaikinMoneyFlow,
    CommodityChannelIndex,
    Momentum,
    MovingAverageConvergenceDivergence,
    OnBalanceVolume,
    RelativeStrengthIndex,
    StochasticOscillator,
    VolumeWeightedMacd,
    WilderAverageTrueRange,
)


DEFAULT_INDICATORS = (
    "rsi",
    "macd",
    "macd_histogram",
    "stochastic",
    "cci",
    "momentum",
    "obv",
    "vwmacd",
    "cmf",
)


class _IndicatorSuite:
    def __init__(self, atr_period: int) -> None:
        self.atr_period = atr_period
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
        self.atr = WilderAverageTrueRange(self.atr_period)

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
        }
        return values, self.atr.update(high, low, close)


class MultiDivergenceReversalStrategy:
    name = "multi-divergence-reversal-v1"
    version = "1"

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        instance_id: str | None = None,
        pivot_period: int = 5,
        max_pivots: int = 16,
        max_bars_to_check: int = 100,
        divergence_types: tuple[str, ...] = ("regular", "hidden"),
        indicators: tuple[str, ...] = DEFAULT_INDICATORS,
        min_entry_divergences: int = 2,
        min_reverse_divergences: int = 2,
        atr_period: int = 14,
        stop_atr_buffer: Decimal = Decimal("0.5"),
        min_stop_atr: Decimal = Decimal("0.5"),
        max_stop_atr: Decimal = Decimal("3"),
        safety_take_profit_rr: Decimal = Decimal("3"),
        risk_usdt: Decimal = Decimal("1"),
        leverage: int = 3,
        margin_utilization: Decimal = Decimal("0.50"),
    ) -> None:
        if interval != "5m":
            raise ValueError("multi-divergence-reversal-v1 requires 5m candles")
        unknown = set(indicators) - set(DEFAULT_INDICATORS)
        if unknown or not indicators:
            raise ValueError(f"unknown or empty divergence indicators: {sorted(unknown)}")
        if min_entry_divergences < 2 or min_reverse_divergences < 2:
            raise ValueError("entry and reverse divergence thresholds must be at least 2")
        if min_entry_divergences > len(indicators) or min_reverse_divergences > len(indicators):
            raise ValueError("divergence threshold exceeds enabled indicator count")
        values = (
            Decimal(stop_atr_buffer),
            Decimal(min_stop_atr),
            Decimal(max_stop_atr),
            Decimal(safety_take_profit_rr),
            Decimal(risk_usdt),
            Decimal(margin_utilization),
        )
        if (
            any(value <= 0 for value in values[:-1])
            or not Decimal("0") < values[-1] <= Decimal("1")
        ):
            raise ValueError("divergence risk and ATR parameters are invalid")
        if Decimal(min_stop_atr) > Decimal(max_stop_atr):
            raise ValueError("minimum stop ATR cannot exceed maximum stop ATR")
        if leverage < 1:
            raise ValueError("strategy leverage must be positive")
        self.symbol = symbol.upper()
        self.interval = interval
        self.instance_id = instance_id or self.name
        self.indicators = tuple(indicators)
        self.pivot_period = pivot_period
        self.max_pivots = max_pivots
        self.max_bars_to_check = max_bars_to_check
        self.divergence_types = tuple(divergence_types)
        self.min_entry_divergences = min_entry_divergences
        self.min_reverse_divergences = min_reverse_divergences
        self.atr_period = atr_period
        self.stop_atr_buffer = Decimal(stop_atr_buffer)
        self.min_stop_atr = Decimal(min_stop_atr)
        self.max_stop_atr = Decimal(max_stop_atr)
        self.safety_take_profit_rr = Decimal(safety_take_profit_rr)
        self.risk_usdt = Decimal(risk_usdt)
        self.leverage = leverage
        self.margin_utilization = Decimal(margin_utilization)
        self.cooldown_bars = 0
        self.reset()

    def reset(self) -> None:
        self._suite = _IndicatorSuite(self.atr_period)
        self._detector = ConfirmedDivergenceDetector(
            indicator_names=self.indicators,
            pivot_period=self.pivot_period,
            max_pivots=self.max_pivots,
            max_bars_to_check=self.max_bars_to_check,
            divergence_types=self.divergence_types,
        )
        self._position = "FLAT"
        self._last_open_time: int | None = None
        self._emitted: set[str] = set()
        self.last_observation: DivergenceObservation | None = None

    def set_position(self, position: str) -> None:
        if position not in {"FLAT", "LONG", "SHORT"}:
            raise ValueError("strategy position must be FLAT, LONG or SHORT")
        self._position = position

    def on_candle(self, candle: Candle) -> StrategyDecision | None:
        if not candle.closed:
            raise ValueError("strategy accepts closed candles only")
        if candle.symbol.upper() != self.symbol or candle.interval != self.interval:
            raise ValueError("candle does not match strategy symbol and interval")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError("strategy candles must be strictly increasing")
        self._last_open_time = candle.open_time
        indicator_values, atr = self._suite.update(candle)
        observation = self._detector.update(candle, indicator_values)
        self.last_observation = observation
        if observation is None or atr is None:
            return None
        bullish = observation.bullish_count
        bearish = observation.bearish_count
        if bullish >= self.min_entry_divergences and bearish >= self.min_entry_divergences:
            return None

        target: str | None = None
        action: str | None = None
        evidence = ()
        if self._position == "FLAT":
            if bullish >= self.min_entry_divergences and bearish < self.min_entry_divergences:
                target, action, evidence = "LONG", "ENTER", observation.bullish_evidence
            elif bearish >= self.min_entry_divergences and bullish < self.min_entry_divergences:
                target, action, evidence = "SHORT", "ENTER", observation.bearish_evidence
        elif self._position == "LONG":
            if bearish >= self.min_reverse_divergences and bullish < self.min_reverse_divergences:
                target, action, evidence = "SHORT", "REVERSE", observation.bearish_evidence
        elif self._position == "SHORT":
            if bullish >= self.min_reverse_divergences and bearish < self.min_reverse_divergences:
                target, action, evidence = "LONG", "REVERSE", observation.bullish_evidence
        if target is None or action is None:
            return None

        signal = self._entry_signal(candle, target, evidence, bullish, bearish, atr)
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
            bullish_count=bullish,
            bearish_count=bearish,
            evidence=tuple(
                observation.bullish_evidence + observation.bearish_evidence
            ),
            entry_signal=signal,
            reason=(
                f"{action.lower()} {target.lower()} on {bullish} bullish and "
                f"{bearish} bearish confirmed divergence indicators"
            ),
        )
        if decision.decision_id in self._emitted:
            return None
        self._emitted.add(decision.decision_id)
        return decision

    def _entry_signal(
        self,
        candle: Candle,
        target: str,
        evidence: tuple,
        bullish_count: int,
        bearish_count: int,
        atr: Decimal,
    ) -> StrategySignal | None:
        reference = Decimal(candle.close)
        if target == "LONG":
            anchor = min(item.current_price for item in evidence)
            stop = anchor - atr * self.stop_atr_buffer
            distance = reference - stop
            side = "BUY"
            take_profit = reference + distance * self.safety_take_profit_rr
        else:
            anchor = max(item.current_price for item in evidence)
            stop = anchor + atr * self.stop_atr_buffer
            distance = stop - reference
            side = "SELL"
            take_profit = reference - distance * self.safety_take_profit_rr
        if stop <= 0 or take_profit <= 0 or distance <= 0:
            return None
        if distance < atr * self.min_stop_atr or distance > atr * self.max_stop_atr:
            return None
        evidence_json = json.dumps(
            [item.as_dict() for item in evidence], sort_keys=True, separators=(",", ":")
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
            take_profit_price=take_profit,
            risk_usdt=self.risk_usdt,
            leverage=self.leverage,
            margin_utilization=self.margin_utilization,
            indicators=(
                ("bullish_count", str(bullish_count)),
                ("bearish_count", str(bearish_count)),
                ("atr", str(atr)),
                ("pivot_period", str(self.pivot_period)),
                ("evidence", evidence_json),
            ),
            reason=f"confirmed {target.lower()} multi-indicator divergence",
            instance_id=self.instance_id,
        )
