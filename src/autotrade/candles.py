from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    trade_count: int
    closed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "trade_count": self.trade_count,
            "closed": self.closed,
        }

    @classmethod
    def from_rest(cls, symbol: str, interval: str, row: list[Any]) -> "Candle":
        return cls(
            symbol=symbol.upper(),
            interval=interval,
            open_time=int(row[0]),
            close_time=int(row[6]),
            open=str(row[1]),
            high=str(row[2]),
            low=str(row[3]),
            close=str(row[4]),
            volume=str(row[5]),
            trade_count=int(row[8]),
            closed=True,
        )

    @classmethod
    def from_stream(cls, payload: dict[str, Any]) -> "Candle":
        kline = payload["k"]
        return cls(
            symbol=str(kline["s"]),
            interval=str(kline["i"]),
            open_time=int(kline["t"]),
            close_time=int(kline["T"]),
            open=str(kline["o"]),
            high=str(kline["h"]),
            low=str(kline["l"]),
            close=str(kline["c"]),
            volume=str(kline["v"]),
            trade_count=int(kline["n"]),
            closed=bool(kline["x"]),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Candle":
        return cls(
            symbol=str(payload["symbol"]).upper(),
            interval=str(payload["interval"]),
            open_time=int(payload["open_time"]),
            close_time=int(payload["close_time"]),
            open=str(payload["open"]),
            high=str(payload["high"]),
            low=str(payload["low"]),
            close=str(payload["close"]),
            volume=str(payload["volume"]),
            trade_count=int(payload["trade_count"]),
            closed=bool(payload["closed"]),
        )
