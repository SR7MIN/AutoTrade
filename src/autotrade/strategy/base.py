from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
from typing import Protocol, TypeAlias

from ..candles import Candle


@dataclass(frozen=True, slots=True)
class StrategySignal:
    strategy: str
    version: str
    symbol: str
    interval: str
    candle_open_time: int
    candle_close_time: int
    side: str
    reference_price: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    risk_usdt: Decimal
    leverage: int
    margin_utilization: Decimal
    indicators: tuple[tuple[str, str], ...]
    reason: str
    instance_id: str | None = None

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("strategy signal side must be BUY or SELL")
        if self.risk_usdt <= 0 or self.leverage < 1:
            raise ValueError("strategy signal risk and leverage must be positive")
        if not Decimal("0") < self.margin_utilization <= Decimal("1"):
            raise ValueError("strategy signal margin utilization must be in (0, 1]")
        if self.side == "BUY":
            if self.stop_price is not None and self.stop_price >= self.reference_price:
                raise ValueError("BUY signal stop must be below the reference price")
            if (
                self.take_profit_price is not None
                and self.take_profit_price <= self.reference_price
            ):
                raise ValueError("BUY signal target must be above the reference price")
        if self.side == "SELL":
            if self.stop_price is not None and self.stop_price <= self.reference_price:
                raise ValueError("SELL signal stop must be above the reference price")
            if (
                self.take_profit_price is not None
                and self.take_profit_price >= self.reference_price
            ):
                raise ValueError("SELL signal target must be below the reference price")

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "symbol": self.symbol,
            "interval": self.interval,
            "candleOpenTime": self.candle_open_time,
            "candleCloseTime": self.candle_close_time,
            "side": self.side,
            "referencePrice": str(self.reference_price),
            "stopPrice": str(self.stop_price) if self.stop_price is not None else None,
            "takeProfitPrice": (
                str(self.take_profit_price)
                if self.take_profit_price is not None
                else None
            ),
            "riskUsdt": str(self.risk_usdt),
            "leverage": self.leverage,
            "marginUtilization": str(self.margin_utilization),
            "indicators": dict(self.indicators),
            "reason": self.reason,
            "instanceId": self.instance_id,
        }

    @property
    def signal_id(self) -> str:
        identity = ":".join(
            (
                self.instance_id or self.strategy,
                self.version,
                self.symbol,
                self.interval,
                str(self.candle_close_time),
                self.side,
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "StrategySignal":
        indicators = payload.get("indicators") or {}
        if not isinstance(indicators, dict):
            raise ValueError("signal indicators must be an object")
        return cls(
            strategy=str(payload["strategy"]),
            version=str(payload["version"]),
            symbol=str(payload["symbol"]).upper(),
            interval=str(payload["interval"]),
            candle_open_time=int(payload["candleOpenTime"]),
            candle_close_time=int(payload["candleCloseTime"]),
            side=str(payload["side"]).upper(),
            reference_price=Decimal(str(payload["referencePrice"])),
            stop_price=(
                Decimal(str(payload["stopPrice"]))
                if payload.get("stopPrice") is not None
                else None
            ),
            take_profit_price=(
                Decimal(str(payload["takeProfitPrice"]))
                if payload.get("takeProfitPrice") is not None
                else None
            ),
            risk_usdt=Decimal(str(payload["riskUsdt"])),
            leverage=int(payload["leverage"]),
            margin_utilization=Decimal(str(payload["marginUtilization"])),
            indicators=tuple((str(key), str(value)) for key, value in indicators.items()),
            reason=str(payload["reason"]),
            instance_id=(
                str(payload["instanceId"])
                if payload.get("instanceId") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DivergenceEvidence:
    indicator: str
    divergence_type: str
    direction: str
    current_pivot_time: int
    previous_pivot_time: int
    current_price: Decimal
    previous_price: Decimal
    current_indicator: Decimal
    previous_indicator: Decimal

    def __post_init__(self) -> None:
        if self.divergence_type not in {"REGULAR", "HIDDEN"}:
            raise ValueError("divergence type must be REGULAR or HIDDEN")
        if self.direction not in {"BULLISH", "BEARISH"}:
            raise ValueError("divergence direction must be BULLISH or BEARISH")

    def as_dict(self) -> dict[str, object]:
        return {
            "indicator": self.indicator,
            "divergenceType": self.divergence_type,
            "direction": self.direction,
            "currentPivotTime": self.current_pivot_time,
            "previousPivotTime": self.previous_pivot_time,
            "currentPrice": str(self.current_price),
            "previousPrice": str(self.previous_price),
            "currentIndicator": str(self.current_indicator),
            "previousIndicator": str(self.previous_indicator),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "DivergenceEvidence":
        return cls(
            indicator=str(value["indicator"]),
            divergence_type=str(value["divergenceType"]),
            direction=str(value["direction"]),
            current_pivot_time=int(value["currentPivotTime"]),
            previous_pivot_time=int(value["previousPivotTime"]),
            current_price=Decimal(str(value["currentPrice"])),
            previous_price=Decimal(str(value["previousPrice"])),
            current_indicator=Decimal(str(value["currentIndicator"])),
            previous_indicator=Decimal(str(value["previousIndicator"])),
        )


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy: str
    version: str
    instance_id: str
    symbol: str
    interval: str
    candle_open_time: int
    candle_close_time: int
    action: str
    current_position: str
    target_position: str
    bullish_count: int
    bearish_count: int
    evidence: tuple[DivergenceEvidence, ...]
    entry_signal: StrategySignal
    reason: str

    def __post_init__(self) -> None:
        if self.action not in {"ENTER", "REVERSE"}:
            raise ValueError("strategy decision action must be ENTER or REVERSE")
        if self.current_position not in {"FLAT", "LONG", "SHORT"}:
            raise ValueError("invalid current strategy position")
        if self.target_position not in {"LONG", "SHORT"}:
            raise ValueError("strategy decision target must be LONG or SHORT")
        expected_side = "BUY" if self.target_position == "LONG" else "SELL"
        if self.entry_signal.side != expected_side:
            raise ValueError("decision target does not match entry signal side")
        if self.action == "ENTER" and self.current_position != "FLAT":
            raise ValueError("ENTER decision requires a flat current position")
        if self.action == "REVERSE" and self.current_position == "FLAT":
            raise ValueError("REVERSE decision requires an open current position")
        if self.action == "REVERSE" and self.current_position == self.target_position:
            raise ValueError("REVERSE target must oppose current position")
        if self.version in {"1", "3", "4", "5"}:
            # Preserve deserialization of decisions emitted by the old
            # approximate detector.
            bullish = len(
                {item.indicator for item in self.evidence if item.direction == "BULLISH"}
            )
            bearish = len(
                {item.indicator for item in self.evidence if item.direction == "BEARISH"}
            )
        else:
            # TradingView counts each indicator/divergence-type slot. One
            # indicator may contribute both regular and hidden slots.
            bullish = sum(item.direction == "BULLISH" for item in self.evidence)
            bearish = sum(item.direction == "BEARISH" for item in self.evidence)
        if bullish != self.bullish_count or bearish != self.bearish_count:
            raise ValueError("decision divergence counts do not match evidence")

    @property
    def decision_id(self) -> str:
        evidence_identity = ",".join(
            f"{item.indicator}:{item.divergence_type}:{item.direction}:"
            f"{item.current_pivot_time}:{item.previous_pivot_time}"
            for item in sorted(
                self.evidence,
                key=lambda item: (
                    item.indicator,
                    item.divergence_type,
                    item.previous_pivot_time,
                ),
            )
        )
        identity = ":".join(
            (
                self.instance_id,
                self.version,
                self.symbol,
                self.interval,
                str(self.candle_close_time),
                self.action,
                self.current_position,
                self.target_position,
                evidence_identity,
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "instanceId": self.instance_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "candleOpenTime": self.candle_open_time,
            "candleCloseTime": self.candle_close_time,
            "action": self.action,
            "currentPosition": self.current_position,
            "targetPosition": self.target_position,
            "bullishCount": self.bullish_count,
            "bearishCount": self.bearish_count,
            "evidence": [item.as_dict() for item in self.evidence],
            "entrySignal": self.entry_signal.as_dict(),
            "reason": self.reason,
            "decisionId": self.decision_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "StrategyDecision":
        evidence = value.get("evidence") or []
        if not isinstance(evidence, list):
            raise ValueError("decision evidence must be an array")
        signal = value.get("entrySignal")
        if not isinstance(signal, dict):
            raise ValueError("decision entry signal must be an object")
        result = cls(
            strategy=str(value["strategy"]),
            version=str(value["version"]),
            instance_id=str(value["instanceId"]),
            symbol=str(value["symbol"]).upper(),
            interval=str(value["interval"]),
            candle_open_time=int(value["candleOpenTime"]),
            candle_close_time=int(value["candleCloseTime"]),
            action=str(value["action"]),
            current_position=str(value["currentPosition"]),
            target_position=str(value["targetPosition"]),
            bullish_count=int(value["bullishCount"]),
            bearish_count=int(value["bearishCount"]),
            evidence=tuple(DivergenceEvidence.from_dict(item) for item in evidence),
            entry_signal=StrategySignal.from_dict(signal),
            reason=str(value["reason"]),
        )
        if value.get("decisionId") not in {None, result.decision_id}:
            raise ValueError("strategy decision ID does not match content")
        return result


@dataclass(frozen=True, slots=True)
class StrategyExitDecision:
    strategy: str
    version: str
    instance_id: str
    symbol: str
    interval: str
    candle_open_time: int
    candle_close_time: int
    current_position: str
    bullish_count: int
    bearish_count: int
    evidence: tuple[DivergenceEvidence, ...]
    reason: str
    action: str = "EXIT"
    target_position: str = "FLAT"

    def __post_init__(self) -> None:
        if self.action != "EXIT" or self.target_position != "FLAT":
            raise ValueError("exit decision must target FLAT")
        if self.current_position not in {"LONG", "SHORT"}:
            raise ValueError("exit decision requires an open current position")
        bullish = len(
            {item.indicator for item in self.evidence if item.direction == "BULLISH"}
        )
        bearish = len(
            {item.indicator for item in self.evidence if item.direction == "BEARISH"}
        )
        if bullish != self.bullish_count or bearish != self.bearish_count:
            raise ValueError("exit divergence counts do not match evidence")

    @property
    def decision_id(self) -> str:
        evidence_identity = ",".join(
            f"{item.indicator}:{item.divergence_type}:{item.direction}:"
            f"{item.current_pivot_time}:{item.previous_pivot_time}"
            for item in sorted(
                self.evidence,
                key=lambda item: (
                    item.indicator,
                    item.divergence_type,
                    item.previous_pivot_time,
                ),
            )
        )
        identity = ":".join(
            (
                self.instance_id,
                self.version,
                self.symbol,
                self.interval,
                str(self.candle_close_time),
                self.action,
                self.current_position,
                evidence_identity,
                self.reason,
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "instanceId": self.instance_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "candleOpenTime": self.candle_open_time,
            "candleCloseTime": self.candle_close_time,
            "action": self.action,
            "currentPosition": self.current_position,
            "targetPosition": self.target_position,
            "bullishCount": self.bullish_count,
            "bearishCount": self.bearish_count,
            "evidence": [item.as_dict() for item in self.evidence],
            "reason": self.reason,
            "decisionId": self.decision_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "StrategyExitDecision":
        evidence = value.get("evidence") or []
        if not isinstance(evidence, list):
            raise ValueError("exit decision evidence must be an array")
        result = cls(
            strategy=str(value["strategy"]),
            version=str(value["version"]),
            instance_id=str(value["instanceId"]),
            symbol=str(value["symbol"]).upper(),
            interval=str(value["interval"]),
            candle_open_time=int(value["candleOpenTime"]),
            candle_close_time=int(value["candleCloseTime"]),
            current_position=str(value["currentPosition"]),
            bullish_count=int(value["bullishCount"]),
            bearish_count=int(value["bearishCount"]),
            evidence=tuple(DivergenceEvidence.from_dict(item) for item in evidence),
            reason=str(value["reason"]),
        )
        if value.get("decisionId") not in {None, result.decision_id}:
            raise ValueError("exit decision ID does not match content")
        return result


StrategyOutput: TypeAlias = StrategySignal | StrategyDecision | StrategyExitDecision


class Strategy(Protocol):
    name: str
    version: str
    symbol: str
    interval: str
    instance_id: str

    def reset(self) -> None: ...

    def on_candle(self, candle: Candle) -> StrategyOutput | None: ...
