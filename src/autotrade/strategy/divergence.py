# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Divergence calculation is a Python port of "Divergence for Many
# Indicators v4", Copyright LonesomeTheBlue.

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
        # Pine counts every non-zero indicator/type slot, not unique indicators.
        return len(self.bullish_evidence)

    @property
    def bearish_count(self) -> int:
        return len(self.bearish_evidence)


@dataclass(frozen=True, slots=True)
class _PricePivot:
    candidate_index: int
    value: Decimal


class ConfirmedDivergenceDetector:
    """Streaming port of the Pine v4 divergence calculation.

    The historical class name is retained for compatibility. With
    ``require_confirmation=True`` (the strategy default), the Pine script uses
    bar offset 1 as the current divergence endpoint. Price pivots are confirmed
    independently and merely populate the historical pivot arrays; divergence
    evaluation itself still occurs on every subsequently closed candle.
    """

    _PINE_PIVOT_ARRAY_SIZE = 20

    def __init__(
        self,
        *,
        indicator_names: tuple[str, ...],
        pivot_period: int = 5,
        pivot_source: str = "high_low",
        max_pivots: int = 10,
        max_bars_to_check: int = 100,
        divergence_types: tuple[str, ...] = ("regular", "hidden"),
        minimum_divergences: int = 1,
        require_confirmation: bool = True,
    ) -> None:
        if not 1 <= pivot_period <= 50:
            raise ValueError("pivot period must be between 1 and 50")
        if not 1 <= max_pivots <= self._PINE_PIVOT_ARRAY_SIZE:
            raise ValueError("maximum pivot points must be between 1 and 20")
        if not 30 <= max_bars_to_check <= 200:
            raise ValueError("maximum bars to check must be between 30 and 200")
        if not 1 <= minimum_divergences <= 11:
            raise ValueError("minimum divergences must be between 1 and 11")
        normalized_source = pivot_source.lower().replace("/", "_")
        if normalized_source in {"high_low", "highlow"}:
            normalized_source = "high_low"
        if normalized_source not in {"close", "high_low"}:
            raise ValueError("pivot source must be close or high_low")
        normalized_types = tuple(
            value
            for value in ("regular", "hidden")
            if value in {item.lower() for item in divergence_types}
        )
        if not normalized_types:
            raise ValueError("divergence types must contain regular and/or hidden")
        if not indicator_names:
            raise ValueError("at least one divergence indicator is required")
        self.indicator_names = indicator_names
        self.pivot_period = pivot_period
        self.pivot_source = normalized_source
        self.max_pivots = max_pivots
        self.max_bars_to_check = max_bars_to_check
        self.divergence_types = normalized_types
        self.minimum_divergences = minimum_divergences
        self.require_confirmation = require_confirmation
        self.reset()

    def reset(self) -> None:
        self._candles: list[Candle] = []
        self._values: list[dict[str, Decimal | None]] = []
        self._low_pivots: list[_PricePivot] = []
        self._high_pivots: list[_PricePivot] = []

    def update(
        self, candle: Candle, indicator_values: dict[str, Decimal | None]
    ) -> DivergenceObservation | None:
        missing = set(self.indicator_names) - set(indicator_values)
        if missing:
            raise ValueError(f"missing divergence indicator values: {sorted(missing)}")
        self._candles.append(candle)
        self._values.append(dict(indicator_values))
        current = len(self._candles) - 1

        # The Pine source unshifts newly confirmed pivots before calculate_divs().
        candidate = current - self.pivot_period
        if candidate >= self.pivot_period:
            if self._is_high_pivot(candidate):
                self._high_pivots.insert(
                    0, _PricePivot(candidate, self._pivot_price(candidate, "HIGH"))
                )
                del self._high_pivots[self._PINE_PIVOT_ARRAY_SIZE :]
            if self._is_low_pivot(candidate):
                self._low_pivots.insert(
                    0, _PricePivot(candidate, self._pivot_price(candidate, "LOW"))
                )
                del self._low_pivots[self._PINE_PIVOT_ARRAY_SIZE :]

        bullish: list[DivergenceEvidence] = []
        bearish: list[DivergenceEvidence] = []
        for indicator in self.indicator_names:
            if "regular" in self.divergence_types:
                item = self._positive_divergence(current, indicator, "REGULAR")
                if item is not None:
                    bullish.append(item)
                item = self._negative_divergence(current, indicator, "REGULAR")
                if item is not None:
                    bearish.append(item)
            if "hidden" in self.divergence_types:
                item = self._positive_divergence(current, indicator, "HIDDEN")
                if item is not None:
                    bullish.append(item)
                item = self._negative_divergence(current, indicator, "HIDDEN")
                if item is not None:
                    bearish.append(item)

        # Pine applies showlimit to all 44 slots together before drawing/alerts.
        if len(bullish) + len(bearish) < self.minimum_divergences:
            return None
        if not bullish and not bearish:
            return None
        return DivergenceObservation(
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            bullish_evidence=tuple(bullish),
            bearish_evidence=tuple(bearish),
        )

    def _pivot_series_value(self, index: int, kind: str) -> Decimal:
        candle = self._candles[index]
        if self.pivot_source == "close":
            return Decimal(candle.close)
        return Decimal(candle.high if kind == "HIGH" else candle.low)

    def _pivot_price(self, index: int, kind: str) -> Decimal:
        return self._pivot_series_value(index, kind)

    def _is_low_pivot(self, index: int) -> bool:
        period = self.pivot_period
        values = [
            self._pivot_series_value(position, "LOW")
            for position in range(index - period, index + period + 1)
        ]
        center = values[period]
        return center == min(values) and any(center < value for value in values)

    def _is_high_pivot(self, index: int) -> bool:
        period = self.pivot_period
        values = [
            self._pivot_series_value(position, "HIGH")
            for position in range(index - period, index + period + 1)
        ]
        center = values[period]
        return center == max(values) and any(center > value for value in values)

    def _positive_divergence(
        self, current: int, indicator: str, divergence_type: str
    ) -> DivergenceEvidence | None:
        # Pine function positive_regular_positive_hidden_divergence().
        if current < 1:
            return None
        source_now = self._indicator(indicator, current)
        source_previous = self._indicator(indicator, current - 1)
        if not (
            not self.require_confirmation
            or self._greater(source_now, source_previous)
            or Decimal(self._candles[current].close)
            > Decimal(self._candles[current - 1].close)
        ):
            return None
        startpoint = 1 if self.require_confirmation else 0
        endpoint = current - startpoint
        endpoint_value = self._indicator(indicator, endpoint)
        if endpoint_value is None:
            return None
        endpoint_price = self._price_source(endpoint, "BULLISH")

        for pivot in self._low_pivots[: self.max_pivots]:
            length = current - pivot.candidate_index
            if length > self.max_bars_to_check:
                break
            if length <= 5:
                continue
            pivot_value = self._indicator(indicator, pivot.candidate_index)
            if pivot_value is None:
                continue
            if divergence_type == "REGULAR":
                matches = endpoint_value > pivot_value and endpoint_price < pivot.value
            else:
                matches = endpoint_value < pivot_value and endpoint_price > pivot.value
            if matches and self._line_is_clear(
                current, startpoint, length, indicator, "BULLISH"
            ):
                return self._evidence(
                    indicator,
                    divergence_type,
                    "BULLISH",
                    pivot.candidate_index,
                    endpoint,
                    pivot.value,
                    endpoint_price,
                    pivot_value,
                    endpoint_value,
                )
        return None

    def _negative_divergence(
        self, current: int, indicator: str, divergence_type: str
    ) -> DivergenceEvidence | None:
        # Pine function negative_regular_negative_hidden_divergence().
        if current < 1:
            return None
        source_now = self._indicator(indicator, current)
        source_previous = self._indicator(indicator, current - 1)
        if not (
            not self.require_confirmation
            or self._less(source_now, source_previous)
            or Decimal(self._candles[current].close)
            < Decimal(self._candles[current - 1].close)
        ):
            return None
        startpoint = 1 if self.require_confirmation else 0
        endpoint = current - startpoint
        endpoint_value = self._indicator(indicator, endpoint)
        if endpoint_value is None:
            return None
        endpoint_price = self._price_source(endpoint, "BEARISH")

        for pivot in self._high_pivots[: self.max_pivots]:
            length = current - pivot.candidate_index
            if length > self.max_bars_to_check:
                break
            if length <= 5:
                continue
            pivot_value = self._indicator(indicator, pivot.candidate_index)
            if pivot_value is None:
                continue
            if divergence_type == "REGULAR":
                matches = endpoint_value < pivot_value and endpoint_price > pivot.value
            else:
                matches = endpoint_value > pivot_value and endpoint_price < pivot.value
            if matches and self._line_is_clear(
                current, startpoint, length, indicator, "BEARISH"
            ):
                return self._evidence(
                    indicator,
                    divergence_type,
                    "BEARISH",
                    pivot.candidate_index,
                    endpoint,
                    pivot.value,
                    endpoint_price,
                    pivot_value,
                    endpoint_value,
                )
        return None

    def _price_source(self, index: int, direction: str) -> Decimal:
        candle = self._candles[index]
        if self.pivot_source == "close":
            return Decimal(candle.close)
        return Decimal(candle.low if direction == "BULLISH" else candle.high)

    def _line_is_clear(
        self,
        current: int,
        startpoint: int,
        length: int,
        indicator: str,
        direction: str,
    ) -> bool:
        endpoint = current - startpoint
        pivot = current - length
        source_endpoint = self._indicator(indicator, endpoint)
        source_pivot = self._indicator(indicator, pivot)
        if source_endpoint is None or source_pivot is None:
            return False
        denominator = Decimal(length - startpoint)
        indicator_slope = (source_endpoint - source_pivot) / denominator
        indicator_line = source_endpoint - indicator_slope
        close_endpoint = Decimal(self._candles[endpoint].close)
        close_pivot = Decimal(self._candles[pivot].close)
        price_slope = (close_endpoint - close_pivot) / denominator
        price_line = close_endpoint - price_slope

        for offset in range(1 + startpoint, length):
            index = current - offset
            source_value = self._indicator(indicator, index)
            close_value = Decimal(self._candles[index].close)
            if direction == "BULLISH":
                # A Pine comparison involving na does not enter the if branch.
                if source_value is not None and source_value < indicator_line:
                    return False
                if close_value < price_line:
                    return False
            else:
                if source_value is not None and source_value > indicator_line:
                    return False
                if close_value > price_line:
                    return False
            indicator_line -= indicator_slope
            price_line -= price_slope
        return True

    def _indicator(self, name: str, index: int) -> Decimal | None:
        if index < 0 or index >= len(self._values):
            return None
        return self._values[index][name]

    @staticmethod
    def _greater(left: Decimal | None, right: Decimal | None) -> bool:
        return left is not None and right is not None and left > right

    @staticmethod
    def _less(left: Decimal | None, right: Decimal | None) -> bool:
        return left is not None and right is not None and left < right

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
