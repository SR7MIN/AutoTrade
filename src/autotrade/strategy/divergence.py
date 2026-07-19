from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..candles import Candle
from .base import DivergenceEvidence


@dataclass(frozen=True, slots=True)
class DivergenceObservation:
    candle_open_time: int
    candle_close_time: int
    bullish_evidence: tuple[DivergenceEvidence, ...]
    bearish_evidence: tuple[DivergenceEvidence, ...]

    @property
    def bullish_count(self) -> int:
        return len({item.indicator for item in self.bullish_evidence})

    @property
    def bearish_count(self) -> int:
        return len({item.indicator for item in self.bearish_evidence})


class ConfirmedDivergenceDetector:
    """No-lookahead divergence detector that emits only on confirmed pivots."""

    def __init__(
        self,
        *,
        indicator_names: tuple[str, ...],
        pivot_period: int = 5,
        max_pivots: int = 16,
        max_bars_to_check: int = 100,
        divergence_types: tuple[str, ...] = ("regular", "hidden"),
    ) -> None:
        if pivot_period < 1:
            raise ValueError("pivot period must be positive")
        if max_pivots < 1 or max_bars_to_check < pivot_period * 2 + 1:
            raise ValueError("divergence pivot and bar limits are invalid")
        normalized_types = tuple(sorted({value.lower() for value in divergence_types}))
        if not normalized_types or set(normalized_types) - {"regular", "hidden"}:
            raise ValueError("divergence types must contain regular and/or hidden")
        if not indicator_names:
            raise ValueError("at least one divergence indicator is required")
        self.indicator_names = indicator_names
        self.pivot_period = pivot_period
        self.max_pivots = max_pivots
        self.max_bars_to_check = max_bars_to_check
        self.divergence_types = normalized_types
        self.reset()

    def reset(self) -> None:
        self._candles: list[Candle] = []
        self._values: list[dict[str, Decimal | None]] = []
        self._low_pivots: list[int] = []
        self._high_pivots: list[int] = []

    def update(
        self, candle: Candle, indicator_values: dict[str, Decimal | None]
    ) -> DivergenceObservation | None:
        missing = set(self.indicator_names) - set(indicator_values)
        if missing:
            raise ValueError(f"missing divergence indicator values: {sorted(missing)}")
        self._candles.append(candle)
        self._values.append(dict(indicator_values))
        candidate = len(self._candles) - 1 - self.pivot_period
        if candidate < self.pivot_period:
            return None

        bullish: list[DivergenceEvidence] = []
        bearish: list[DivergenceEvidence] = []
        if self._is_low_pivot(candidate):
            bullish.extend(self._find_bullish(candidate))
            self._low_pivots.append(candidate)
            self._low_pivots = self._low_pivots[-self.max_pivots :]
        if self._is_high_pivot(candidate):
            bearish.extend(self._find_bearish(candidate))
            self._high_pivots.append(candidate)
            self._high_pivots = self._high_pivots[-self.max_pivots :]
        if not bullish and not bearish:
            return None
        return DivergenceObservation(
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            bullish_evidence=tuple(bullish),
            bearish_evidence=tuple(bearish),
        )

    def _is_low_pivot(self, index: int) -> bool:
        period = self.pivot_period
        values = [
            Decimal(self._candles[position].low)
            for position in range(index - period, index + period + 1)
        ]
        center = values[period]
        return center == min(values) and any(center < value for value in values)

    def _is_high_pivot(self, index: int) -> bool:
        period = self.pivot_period
        values = [
            Decimal(self._candles[position].high)
            for position in range(index - period, index + period + 1)
        ]
        center = values[period]
        return center == max(values) and any(center > value for value in values)

    def _find_bullish(self, current: int) -> list[DivergenceEvidence]:
        result: list[DivergenceEvidence] = []
        current_price = Decimal(self._candles[current].low)
        for indicator in self.indicator_names:
            current_value = self._values[current][indicator]
            if current_value is None:
                continue
            found_types: set[str] = set()
            for previous in reversed(self._low_pivots[-self.max_pivots :]):
                if current - previous > self.max_bars_to_check:
                    continue
                previous_value = self._values[previous][indicator]
                if previous_value is None:
                    continue
                previous_price = Decimal(self._candles[previous].low)
                divergence_type: str | None = None
                if (
                    "regular" in self.divergence_types
                    and current_price < previous_price
                    and current_value > previous_value
                ):
                    divergence_type = "REGULAR"
                elif (
                    "hidden" in self.divergence_types
                    and current_price > previous_price
                    and current_value < previous_value
                ):
                    divergence_type = "HIDDEN"
                if divergence_type is None or divergence_type in found_types:
                    continue
                if not self._price_line_clear(previous, current, "BULLISH"):
                    continue
                if not self._indicator_line_clear(
                    indicator, previous, current, "BULLISH"
                ):
                    continue
                result.append(
                    self._evidence(
                        indicator,
                        divergence_type,
                        "BULLISH",
                        previous,
                        current,
                        previous_price,
                        current_price,
                        previous_value,
                        current_value,
                    )
                )
                found_types.add(divergence_type)
                if len(found_types) == len(self.divergence_types):
                    break
        return result

    def _find_bearish(self, current: int) -> list[DivergenceEvidence]:
        result: list[DivergenceEvidence] = []
        current_price = Decimal(self._candles[current].high)
        for indicator in self.indicator_names:
            current_value = self._values[current][indicator]
            if current_value is None:
                continue
            found_types: set[str] = set()
            for previous in reversed(self._high_pivots[-self.max_pivots :]):
                if current - previous > self.max_bars_to_check:
                    continue
                previous_value = self._values[previous][indicator]
                if previous_value is None:
                    continue
                previous_price = Decimal(self._candles[previous].high)
                divergence_type: str | None = None
                if (
                    "regular" in self.divergence_types
                    and current_price > previous_price
                    and current_value < previous_value
                ):
                    divergence_type = "REGULAR"
                elif (
                    "hidden" in self.divergence_types
                    and current_price < previous_price
                    and current_value > previous_value
                ):
                    divergence_type = "HIDDEN"
                if divergence_type is None or divergence_type in found_types:
                    continue
                if not self._price_line_clear(previous, current, "BEARISH"):
                    continue
                if not self._indicator_line_clear(
                    indicator, previous, current, "BEARISH"
                ):
                    continue
                result.append(
                    self._evidence(
                        indicator,
                        divergence_type,
                        "BEARISH",
                        previous,
                        current,
                        previous_price,
                        current_price,
                        previous_value,
                        current_value,
                    )
                )
                found_types.add(divergence_type)
                if len(found_types) == len(self.divergence_types):
                    break
        return result

    def _price_line_clear(self, start: int, end: int, direction: str) -> bool:
        if end - start <= 1:
            return True
        first = Decimal(
            self._candles[start].low
            if direction == "BULLISH"
            else self._candles[start].high
        )
        last = Decimal(
            self._candles[end].low
            if direction == "BULLISH"
            else self._candles[end].high
        )
        for index in range(start + 1, end):
            fraction = Decimal(index - start) / Decimal(end - start)
            line = first + (last - first) * fraction
            value = Decimal(
                self._candles[index].low
                if direction == "BULLISH"
                else self._candles[index].high
            )
            if direction == "BULLISH" and value < line:
                return False
            if direction == "BEARISH" and value > line:
                return False
        return True

    def _indicator_line_clear(
        self, indicator: str, start: int, end: int, direction: str
    ) -> bool:
        first = self._values[start][indicator]
        last = self._values[end][indicator]
        if first is None or last is None:
            return False
        for index in range(start + 1, end):
            value = self._values[index][indicator]
            if value is None:
                return False
            fraction = Decimal(index - start) / Decimal(end - start)
            line = first + (last - first) * fraction
            if direction == "BULLISH" and value < line:
                return False
            if direction == "BEARISH" and value > line:
                return False
        return True

    def _evidence(
        self,
        indicator: str,
        divergence_type: str,
        direction: str,
        previous: int,
        current: int,
        previous_price: Decimal,
        current_price: Decimal,
        previous_value: Decimal,
        current_value: Decimal,
    ) -> DivergenceEvidence:
        return DivergenceEvidence(
            indicator=indicator,
            divergence_type=divergence_type,
            direction=direction,
            current_pivot_time=self._candles[current].open_time,
            previous_pivot_time=self._candles[previous].open_time,
            current_price=current_price,
            previous_price=previous_price,
            current_indicator=current_value,
            previous_indicator=previous_value,
        )
