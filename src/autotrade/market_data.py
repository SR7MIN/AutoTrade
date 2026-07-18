from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any

import websockets

from .binance_rest import BinanceRestClient
from .journal import OrderJournal


INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


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


class MarketDataService:
    def __init__(
        self,
        client: BinanceRestClient,
        journal: OrderJournal,
        ws_base_url: str,
    ) -> None:
        self.client = client
        self.journal = journal
        self.ws_base_url = ws_base_url

    def backfill(self, symbol: str, interval: str) -> int:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval: {interval}")
        symbol = symbol.upper()
        latest = self.journal.latest_candle_open_time(symbol, interval)
        start = latest + INTERVAL_MS[interval] if latest is not None else None
        rows = self.client.klines(symbol, interval, start_time=start, limit=500)
        server_now = self.client.current_server_time_ms()
        inserted = 0
        for row in rows:
            candle = Candle.from_rest(symbol, interval, row)
            if candle.close_time >= server_now:
                continue
            inserted += int(self.journal.store_candle(candle.as_dict()))
        return inserted

    async def run(self, symbol: str, interval: str) -> None:
        symbol = symbol.upper()
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval: {interval}")
        delay = 1.0
        health_key = f"market_data_{symbol}_{interval}_healthy"
        self.journal.set_control(health_key, "false", "market stream starting")
        while True:
            await asyncio.to_thread(self.backfill, symbol, interval)
            try:
                url = f"{self.ws_base_url}/ws/{symbol.lower()}@kline_{interval}"
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=10
                ) as websocket:
                    self.journal.set_control(
                        health_key, "true", "market stream connected", severity="INFO"
                    )
                    delay = 1.0
                    async for message in websocket:
                        candle = Candle.from_stream(json.loads(message))
                        if not candle.closed:
                            continue
                        latest = self.journal.latest_candle_open_time(symbol, interval)
                        if latest is not None and candle.open_time > latest + INTERVAL_MS[interval]:
                            await asyncio.to_thread(self.backfill, symbol, interval)
                        self.journal.store_candle(candle.as_dict())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.journal.set_control(health_key, "false", f"market stream error: {exc}")
                self.journal.append_audit(
                    "market_data",
                    "STREAM_RECONNECT",
                    symbol=symbol,
                    severity="WARNING",
                    payload={"interval": interval, "error": str(exc), "delay": delay},
                )
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30.0)
