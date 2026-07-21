from __future__ import annotations

import math
from collections import deque
from decimal import Decimal

from ..candles import Candle
from .base import StrategyDecision, StrategyExitDecision, StrategySignal
from .indicators import WilderAverageTrueRange


class PineWeightedMovingAverage:
    """TradingView-style WMA for a valid, non-na source stream."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("WMA period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._values: deque[Decimal] = deque(maxlen=self.period)

    def update(self, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        self._values.append(Decimal(value))
        if len(self._values) < self.period:
            return None
        denominator = Decimal(self.period * (self.period + 1) // 2)
        weighted = sum(
            (item * Decimal(index) for index, item in enumerate(self._values, start=1)),
            Decimal(0),
        )
        return weighted / denominator


class PineHullMovingAverage:
    """HMA(src, length) from the supplied Pine v4 script."""

    def __init__(self, length: int) -> None:
        if length < 2:
            raise ValueError("Hull length must be at least 2")
        self.length = length
        # Pine v4 accepts these integer lengths in the built-in WMA call. For
        # positive integer inputs the effective conversion is truncation.
        self.half_length = max(1, length // 2)
        self.sqrt_length = max(1, int(math.floor(math.sqrt(length) + 0.5)))
        self.reset()

    def reset(self) -> None:
        self._half = PineWeightedMovingAverage(self.half_length)
        self._full = PineWeightedMovingAverage(self.length)
        self._outer = PineWeightedMovingAverage(self.sqrt_length)

    def update(self, source: Decimal) -> Decimal | None:
        source = Decimal(source)
        half = self._half.update(source)
        full = self._full.update(source)
        raw = None if half is None or full is None else Decimal(2) * half - full
        return self._outer.update(raw)


class HullSuiteFullEquityStrategy:
    """Daily HMA target-position strategy using 100% of current equity."""

    name = "hull-suite-full-equity-v1"
    version = "1"

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        instance_id: str | None = None,
        direction: str = "all",
        protective_stop_enabled: bool = False,
        length: int = 55,
        atr_period: int = 14,
        stop_atr_multiple: Decimal = Decimal("2"),
        structure_lookback: int = 10,
        structure_atr_buffer: Decimal = Decimal("0.25"),
        min_stop_atr: Decimal = Decimal("1.5"),
        max_stop_atr: Decimal = Decimal("3.5"),
        risk_usdt: Decimal = Decimal("1"),
        leverage: int = 1,
        margin_utilization: Decimal = Decimal("1"),
    ) -> None:
        if interval != "1d":
            raise ValueError("hull-suite-full-equity-v1 requires daily candles")
        if direction not in {"long", "all"}:
            raise ValueError("Hull direction must be long or all")
        if length < 2 or atr_period < 1 or structure_lookback < 1:
            raise ValueError("Hull periods must be positive")
        decimals = (
            Decimal(stop_atr_multiple),
            Decimal(structure_atr_buffer),
            Decimal(min_stop_atr),
            Decimal(max_stop_atr),
            Decimal(risk_usdt),
            Decimal(margin_utilization),
        )
        if any(value <= 0 for value in decimals[:5]):
            raise ValueError("Hull risk and stop settings must be positive")
        if not Decimal("0") < decimals[-1] <= Decimal("1"):
            raise ValueError("margin utilization must be in (0, 1]")
        if Decimal(min_stop_atr) > Decimal(max_stop_atr):
            raise ValueError("minimum stop ATR cannot exceed maximum stop ATR")
        if leverage < 1:
            raise ValueError("Hull leverage must be positive")

        self.symbol = symbol.upper()
        self.interval = interval
        self.instance_id = instance_id or self.name
        self.direction = direction
        self.protective_stop_enabled = protective_stop_enabled
        self.length = length
        self.atr_period = atr_period
        self.stop_atr_multiple = Decimal(stop_atr_multiple)
        self.structure_lookback = structure_lookback
        self.structure_atr_buffer = Decimal(structure_atr_buffer)
        self.min_stop_atr = Decimal(min_stop_atr)
        self.max_stop_atr = Decimal(max_stop_atr)
        self.risk_usdt = Decimal(risk_usdt)
        self.leverage = leverage
        self.margin_utilization = Decimal(margin_utilization)
        self.reset()

    def reset(self) -> None:
        self._hull = PineHullMovingAverage(self.length)
        self._atr = WilderAverageTrueRange(self.atr_period)
        self._recent_candles: deque[Candle] = deque(maxlen=self.structure_lookback)
        self._hull_values: deque[Decimal] = deque(maxlen=3)
        self._position = "FLAT"
        self._last_direction: str | None = None
        self._last_open_time: int | None = None

    def set_position(self, position: str) -> None:
        if position not in {"FLAT", "LONG", "SHORT"}:
            raise ValueError("Hull position must be FLAT, LONG or SHORT")
        self._position = position

    def on_candle(
        self, candle: Candle
    ) -> StrategySignal | StrategyDecision | StrategyExitDecision | None:
        if not candle.closed:
            raise ValueError("strategy accepts closed candles only")
        if candle.symbol.upper() != self.symbol or candle.interval != self.interval:
            raise ValueError("candle does not match Hull strategy symbol and interval")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError("strategy candles must be strictly increasing")
        self._last_open_time = candle.open_time

        close = Decimal(candle.close)
        high = Decimal(candle.high)
        low = Decimal(candle.low)
        hull = self._hull.update(close)
        atr = self._atr.update(high, low, close)
        self._recent_candles.append(candle)
        if hull is None or atr is None:
            return None
        self._hull_values.append(hull)
        if len(self._hull_values) < 3:
            return None
        current = self._hull_values[-1]
        two_bars_ago = self._hull_values[0]
        trend = "LONG" if current > two_bars_ago else "SHORT" if current < two_bars_ago else None
        if trend is None or trend == self._last_direction:
            return None
        self._last_direction = trend
        if not self._direction_allowed(trend):
            if self._position != "FLAT" and self._position != trend:
                return self._exit(candle, f"Hull direction {trend.lower()} is disabled")
            return None

        target = trend
        if self._position == target:
            return None
        if self._position != "FLAT":
            signal = self._signal(candle, target, atr)
            if signal is None:
                return None
            return StrategyDecision(
                strategy=self.name,
                version=self.version,
                instance_id=self.instance_id,
                symbol=self.symbol,
                interval=self.interval,
                candle_open_time=candle.open_time,
                candle_close_time=candle.close_time,
                action="REVERSE",
                current_position=self._position,
                target_position=target,
                bullish_count=0,
                bearish_count=0,
                evidence=(),
                entry_signal=signal,
                reason=f"Hull direction changed to {target.lower()}",
            )
        return self._signal(candle, target, atr)

    def _direction_allowed(self, target: str) -> bool:
        return self.direction == "all" or (
            target == "LONG" and self.direction == "long"
        )

    def _signal(
        self, candle: Candle, target: str, atr: Decimal
    ) -> StrategySignal | None:
        reference = Decimal(candle.close)
        if reference <= 0 or len(self._recent_candles) < self.structure_lookback:
            return None
        side = "BUY" if target == "LONG" else "SELL"
        stop: Decimal | None = None
        min_stop_bps: Decimal | None = None
        max_stop_bps: Decimal | None = None
        if self.protective_stop_enabled:
            # Optional protected variant retained from the previous baseline.
            # The default full-equity parity configuration intentionally leaves
            # both stop and take-profit unset.
            protected = self._protective_stop(reference, target, atr)
            if protected is None:
                return None
            stop, min_stop_bps, max_stop_bps = protected
        indicators = [
            ("hull", str(self._hull_values[-1])),
            ("hull_2_bars_ago", str(self._hull_values[0])),
            ("atr", str(atr)),
            ("atr_period", str(self.atr_period)),
            ("length", str(self.length)),
            ("mode", "Hma"),
            ("direction", self.direction),
            ("position_size_mode", "full_equity"),
            ("equity_fraction", "1"),
            ("protective_stop_enabled", str(self.protective_stop_enabled).lower()),
            ("exit_policy", "hull_flip"),
        ]
        if min_stop_bps is not None and max_stop_bps is not None:
            indicators.extend(
                (
                    ("structure_lookback", str(self.structure_lookback)),
                    ("min_stop_atr", str(self.min_stop_atr)),
                    ("max_stop_atr", str(self.max_stop_atr)),
                    ("min_stop_bps", str(min_stop_bps)),
                    ("max_stop_bps", str(max_stop_bps)),
                )
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
            indicators=tuple(indicators),
            reason=f"HMA({self.length}) direction changed to {target.lower()}",
            instance_id=self.instance_id,
        )

    def _protective_stop(
        self, reference: Decimal, target: str, atr: Decimal
    ) -> tuple[Decimal, Decimal, Decimal] | None:
        lows = [Decimal(item.low) for item in self._recent_candles]
        highs = [Decimal(item.high) for item in self._recent_candles]
        if target == "LONG":
            structure_stop = min(lows) - atr * self.structure_atr_buffer
            stop = min(reference - atr * self.stop_atr_multiple, structure_stop)
        else:
            structure_stop = max(highs) + atr * self.structure_atr_buffer
            stop = max(reference + atr * self.stop_atr_multiple, structure_stop)
        distance = abs(reference - stop)
        min_distance = atr * self.min_stop_atr
        max_distance = atr * self.max_stop_atr
        if distance < min_distance:
            distance = min_distance
            stop = reference - distance if target == "LONG" else reference + distance
        if distance > max_distance or stop <= 0:
            return None
        return (
            stop,
            min_distance / reference * Decimal("10000"),
            max_distance / reference * Decimal("10000"),
        )

    def _exit(self, candle: Candle, reason: str) -> StrategyExitDecision:
        return StrategyExitDecision(
            strategy=self.name,
            version=self.version,
            instance_id=self.instance_id,
            symbol=self.symbol,
            interval=self.interval,
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            current_position=self._position,
            bullish_count=0,
            bearish_count=0,
            evidence=(),
            reason=reason,
        )
