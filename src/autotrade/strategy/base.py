from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

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
    stop_price: Decimal
    take_profit_price: Decimal
    risk_usdt: Decimal
    leverage: int
    margin_utilization: Decimal
    indicators: tuple[tuple[str, str], ...]
    reason: str

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("strategy signal side must be BUY or SELL")
        if self.risk_usdt <= 0 or self.leverage < 1:
            raise ValueError("strategy signal risk and leverage must be positive")
        if not Decimal("0") < self.margin_utilization <= Decimal("1"):
            raise ValueError("strategy signal margin utilization must be in (0, 1]")
        if self.side == "BUY" and not (
            self.stop_price < self.reference_price < self.take_profit_price
        ):
            raise ValueError("BUY signal prices are not ordered stop < reference < target")
        if self.side == "SELL" and not (
            self.take_profit_price < self.reference_price < self.stop_price
        ):
            raise ValueError("SELL signal prices are not ordered target < reference < stop")

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
            "stopPrice": str(self.stop_price),
            "takeProfitPrice": str(self.take_profit_price),
            "riskUsdt": str(self.risk_usdt),
            "leverage": self.leverage,
            "marginUtilization": str(self.margin_utilization),
            "indicators": dict(self.indicators),
            "reason": self.reason,
        }


class Strategy(Protocol):
    name: str
    version: str

    def reset(self) -> None: ...

    def on_candle(self, candle: Candle) -> StrategySignal | None: ...
