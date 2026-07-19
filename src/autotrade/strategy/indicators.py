from __future__ import annotations

from decimal import Decimal


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
