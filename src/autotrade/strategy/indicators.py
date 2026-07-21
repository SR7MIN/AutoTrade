from __future__ import annotations

from collections import deque
from decimal import Decimal


class PineExponentialMovingAverage:
    """TradingView/Pine EMA: seed from the first non-na source value."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self._alpha = Decimal(2) / Decimal(period + 1)
        self.value: Decimal | None = None

    def reset(self) -> None:
        self.value = None

    def update(self, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        value = Decimal(value)
        if self.value is None:
            self.value = value
        else:
            self.value = self._alpha * value + (Decimal(1) - self._alpha) * self.value
        return self.value


class PineSimpleMovingAverage:
    """Pine SMA semantics: na source values are ignored."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("SMA period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._values: deque[Decimal] = deque()
        self._total = Decimal(0)

    def update(self, value: Decimal | None) -> Decimal | None:
        if value is not None:
            decimal_value = Decimal(value)
            self._values.append(decimal_value)
            self._total += decimal_value
            if len(self._values) > self.period:
                self._total -= self._values.popleft()
        if len(self._values) < self.period:
            return None
        return self._total / Decimal(self.period)


class ExponentialMovingAverage:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self._multiplier = Decimal(2) / Decimal(period + 1)
        self.reset()

    def reset(self) -> None:
        self._seed: list[Decimal] = []
        self.value: Decimal | None = None

    def update(self, value: Decimal) -> Decimal | None:
        value = Decimal(value)
        if self.value is None:
            self._seed.append(value)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed, Decimal(0)) / Decimal(self.period)
            self._seed.clear()
            return self.value
        self.value = (value - self.value) * self._multiplier + self.value
        return self.value


class WilderAverageTrueRange:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("ATR period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._seed: list[Decimal] = []
        self._previous_close: Decimal | None = None
        self.value: Decimal | None = None

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        high = Decimal(high)
        low = Decimal(low)
        close = Decimal(close)
        if high < low:
            raise ValueError("candle high cannot be below low")
        true_range = high - low
        if self._previous_close is not None:
            true_range = max(
                true_range,
                abs(high - self._previous_close),
                abs(low - self._previous_close),
            )
        self._previous_close = close

        if self.value is None:
            self._seed.append(true_range)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed, Decimal(0)) / Decimal(self.period)
            self._seed.clear()
            return self.value
        self.value = (
            self.value * Decimal(self.period - 1) + true_range
        ) / Decimal(self.period)
        return self.value


class WilderDirectionalMovementIndex:
    """Wilder DMI/ADX with an SMA seed followed by Wilder smoothing."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("ADX period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._previous_high: Decimal | None = None
        self._previous_low: Decimal | None = None
        self._previous_close: Decimal | None = None
        self._tr_seed: list[Decimal] = []
        self._plus_seed: list[Decimal] = []
        self._minus_seed: list[Decimal] = []
        self._dx_seed: list[Decimal] = []
        self._smoothed_tr: Decimal | None = None
        self._smoothed_plus: Decimal | None = None
        self._smoothed_minus: Decimal | None = None
        self.value: Decimal | None = None

    def update(
        self, high: Decimal, low: Decimal, close: Decimal
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        high = Decimal(high)
        low = Decimal(low)
        close = Decimal(close)
        if high < low:
            raise ValueError("candle high cannot be below low")
        if self._previous_close is None:
            self._previous_high = high
            self._previous_low = low
            self._previous_close = close
            return None, None, None

        assert self._previous_high is not None and self._previous_low is not None
        true_range = max(
            high - low,
            abs(high - self._previous_close),
            abs(low - self._previous_close),
        )
        up_move = high - self._previous_high
        down_move = self._previous_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0 else Decimal(0)
        minus_dm = down_move if down_move > up_move and down_move > 0 else Decimal(0)
        self._previous_high = high
        self._previous_low = low
        self._previous_close = close

        if self._smoothed_tr is None:
            self._tr_seed.append(true_range)
            self._plus_seed.append(plus_dm)
            self._minus_seed.append(minus_dm)
            if len(self._tr_seed) < self.period:
                return None, None, None
            self._smoothed_tr = sum(self._tr_seed, Decimal(0))
            self._smoothed_plus = sum(self._plus_seed, Decimal(0))
            self._smoothed_minus = sum(self._minus_seed, Decimal(0))
            self._tr_seed.clear()
            self._plus_seed.clear()
            self._minus_seed.clear()
        else:
            assert self._smoothed_plus is not None and self._smoothed_minus is not None
            divisor = Decimal(self.period)
            self._smoothed_tr = self._smoothed_tr - self._smoothed_tr / divisor + true_range
            self._smoothed_plus = (
                self._smoothed_plus - self._smoothed_plus / divisor + plus_dm
            )
            self._smoothed_minus = (
                self._smoothed_minus - self._smoothed_minus / divisor + minus_dm
            )

        if self._smoothed_tr == 0:
            return self.value, None, None
        plus_di = Decimal(100) * self._smoothed_plus / self._smoothed_tr
        minus_di = Decimal(100) * self._smoothed_minus / self._smoothed_tr
        total = plus_di + minus_di
        dx = Decimal(0) if total == 0 else Decimal(100) * abs(plus_di - minus_di) / total
        if self.value is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) < self.period:
                return None, plus_di, minus_di
            self.value = sum(self._dx_seed, Decimal(0)) / Decimal(self.period)
            self._dx_seed.clear()
        else:
            self.value = (
                self.value * Decimal(self.period - 1) + dx
            ) / Decimal(self.period)
        return self.value, plus_di, minus_di


class SimpleMovingAverage:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("SMA period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._values: deque[Decimal] = deque()
        self._total = Decimal(0)

    def update(self, value: Decimal) -> Decimal | None:
        value = Decimal(value)
        self._values.append(value)
        self._total += value
        if len(self._values) > self.period:
            self._total -= self._values.popleft()
        if len(self._values) < self.period:
            return None
        return self._total / Decimal(self.period)


class RelativeStrengthIndex:
    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("RSI period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._previous: Decimal | None = None
        self._gains: list[Decimal] = []
        self._losses: list[Decimal] = []
        self._average_gain: Decimal | None = None
        self._average_loss: Decimal | None = None

    def update(self, close: Decimal) -> Decimal | None:
        close = Decimal(close)
        if self._previous is None:
            self._previous = close
            return None
        change = close - self._previous
        self._previous = close
        gain = max(change, Decimal(0))
        loss = max(-change, Decimal(0))
        if self._average_gain is None or self._average_loss is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._average_gain = sum(self._gains, Decimal(0)) / Decimal(self.period)
            self._average_loss = sum(self._losses, Decimal(0)) / Decimal(self.period)
            self._gains.clear()
            self._losses.clear()
        else:
            self._average_gain = (
                self._average_gain * Decimal(self.period - 1) + gain
            ) / Decimal(self.period)
            self._average_loss = (
                self._average_loss * Decimal(self.period - 1) + loss
            ) / Decimal(self.period)
        if self._average_loss == 0:
            return Decimal(100) if self._average_gain > 0 else None
        if self._average_gain == 0:
            return Decimal(0)
        relative_strength = self._average_gain / self._average_loss
        return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


class MovingAverageConvergenceDivergence:
    def __init__(
        self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError("MACD fast period must be below slow period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.reset()

    def reset(self) -> None:
        self._fast = PineExponentialMovingAverage(self.fast_period)
        self._slow = PineExponentialMovingAverage(self.slow_period)
        self._signal = PineExponentialMovingAverage(self.signal_period)

    def update(
        self, close: Decimal
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        if fast is None or slow is None:
            return None, None, None
        macd = fast - slow
        signal = self._signal.update(macd)
        return macd, signal, macd - signal if signal is not None else None


class Momentum:
    def __init__(self, period: int = 10) -> None:
        if period < 1:
            raise ValueError("momentum period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._values: deque[Decimal] = deque()

    def update(self, close: Decimal) -> Decimal | None:
        close = Decimal(close)
        self._values.append(close)
        if len(self._values) <= self.period:
            return None
        previous = self._values.popleft()
        return close - previous


class CommodityChannelIndex:
    def __init__(self, period: int = 10) -> None:
        if period < 1:
            raise ValueError("CCI period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._values: deque[Decimal] = deque()

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        typical = (Decimal(high) + Decimal(low) + Decimal(close)) / Decimal(3)
        self._values.append(typical)
        if len(self._values) > self.period:
            self._values.popleft()
        if len(self._values) < self.period:
            return None
        average = sum(self._values, Decimal(0)) / Decimal(self.period)
        mean_deviation = sum(
            (abs(value - average) for value in self._values), Decimal(0)
        ) / Decimal(self.period)
        if mean_deviation == 0:
            return None
        return (typical - average) / (Decimal("0.015") * mean_deviation)


class OnBalanceVolume:
    def reset(self) -> None:
        self._previous_close: Decimal | None = None
        self.value = Decimal(0)

    def __init__(self) -> None:
        self.reset()

    def update(self, close: Decimal, volume: Decimal) -> Decimal:
        close = Decimal(close)
        volume = Decimal(volume)
        if self._previous_close is not None:
            if close > self._previous_close:
                self.value += volume
            elif close < self._previous_close:
                self.value -= volume
        self._previous_close = close
        return self.value


class StochasticOscillator:
    def __init__(self, period: int = 14, smooth_period: int = 3) -> None:
        if period < 1 or smooth_period < 1:
            raise ValueError("stochastic periods must be positive")
        self.period = period
        self.smooth_period = smooth_period
        self.reset()

    def reset(self) -> None:
        self._highs: deque[Decimal] = deque()
        self._lows: deque[Decimal] = deque()
        self._smooth = PineSimpleMovingAverage(self.smooth_period)

    def update(self, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
        self._highs.append(Decimal(high))
        self._lows.append(Decimal(low))
        if len(self._highs) > self.period:
            self._highs.popleft()
            self._lows.popleft()
        if len(self._highs) < self.period:
            return None
        highest = max(self._highs)
        lowest = min(self._lows)
        raw = (
            None
            if highest == lowest
            else Decimal(100) * (Decimal(close) - lowest) / (highest - lowest)
        )
        return self._smooth.update(raw)


class VolumeWeightedMovingAverage:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("VWMA period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._values: deque[tuple[Decimal, Decimal]] = deque()
        self._weighted = Decimal(0)
        self._volume = Decimal(0)

    def update(self, value: Decimal, volume: Decimal) -> Decimal | None:
        value = Decimal(value)
        volume = Decimal(volume)
        self._values.append((value, volume))
        self._weighted += value * volume
        self._volume += volume
        if len(self._values) > self.period:
            old_value, old_volume = self._values.popleft()
            self._weighted -= old_value * old_volume
            self._volume -= old_volume
        if len(self._values) < self.period or self._volume == 0:
            return None
        return self._weighted / self._volume


class VolumeWeightedMacd:
    def __init__(self, fast_period: int = 12, slow_period: int = 26) -> None:
        if fast_period >= slow_period:
            raise ValueError("VWMACD fast period must be below slow period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.reset()

    def reset(self) -> None:
        self._fast = VolumeWeightedMovingAverage(self.fast_period)
        self._slow = VolumeWeightedMovingAverage(self.slow_period)

    def update(self, close: Decimal, volume: Decimal) -> Decimal | None:
        fast = self._fast.update(close, volume)
        slow = self._slow.update(close, volume)
        if fast is None or slow is None:
            return None
        return fast - slow


class ChaikinMoneyFlow:
    def __init__(self, period: int = 21) -> None:
        if period < 1:
            raise ValueError("CMF period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._flow_average = PineSimpleMovingAverage(self.period)
        self._volume_average = PineSimpleMovingAverage(self.period)

    def update(
        self, high: Decimal, low: Decimal, close: Decimal, volume: Decimal
    ) -> Decimal | None:
        high = Decimal(high)
        low = Decimal(low)
        close = Decimal(close)
        volume = Decimal(volume)
        multiplier = (
            None
            if high == low
            else ((close - low) - (high - close)) / (high - low)
        )
        flow = None if multiplier is None else multiplier * volume
        flow_average = self._flow_average.update(flow)
        volume_average = self._volume_average.update(volume)
        if flow_average is None or volume_average in {None, Decimal(0)}:
            return None
        return flow_average / volume_average


class MoneyFlowIndex:
    """Pine v4 ``mfi(source, length)`` using positive/negative raw money flow."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("MFI period must be positive")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._previous: Decimal | None = None
        self._positive: deque[Decimal] = deque()
        self._negative: deque[Decimal] = deque()
        self._positive_total = Decimal(0)
        self._negative_total = Decimal(0)

    def update(self, source: Decimal, volume: Decimal) -> Decimal | None:
        source = Decimal(source)
        volume = Decimal(volume)
        positive = Decimal(0)
        negative = Decimal(0)
        if self._previous is None:
            # In Pine, change(source) is na on the first bar. An na ternary
            # condition selects the false branch in both legacy mfi sums.
            positive = source * volume
            negative = source * volume
        else:
            if source > self._previous:
                positive = source * volume
            elif source < self._previous:
                negative = source * volume
        self._previous = source
        self._positive.append(positive)
        self._negative.append(negative)
        self._positive_total += positive
        self._negative_total += negative
        if len(self._positive) > self.period:
            self._positive_total -= self._positive.popleft()
            self._negative_total -= self._negative.popleft()
        if len(self._positive) < self.period:
            return None
        if self._negative_total == 0:
            return Decimal(100) if self._positive_total > 0 else None
        if self._positive_total == 0:
            return Decimal(0)
        ratio = self._positive_total / self._negative_total
        return Decimal(100) - Decimal(100) / (Decimal(1) + ratio)
