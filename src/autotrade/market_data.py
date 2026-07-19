from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any

import websockets

from .binance_rest import BinanceRestClient
from .candles import Candle
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
class CandleGap:
    start_time: int
    end_time: int
    missing: int

    def as_dict(self) -> dict[str, int]:
        return {
            "startTime": self.start_time,
            "endTime": self.end_time,
            "missing": self.missing,
        }


@dataclass(frozen=True, slots=True)
class BackfillResult:
    symbol: str
    interval: str
    start_time: int
    end_time: int
    pages: int
    rows_received: int
    inserted: int
    existing: int
    exchange_duplicates: int
    gaps: tuple[CandleGap, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "pages": self.pages,
            "rowsReceived": self.rows_received,
            "inserted": self.inserted,
            "existing": self.existing,
            "exchangeDuplicates": self.exchange_duplicates,
            "gapCount": sum(gap.missing for gap in self.gaps),
            "gaps": [gap.as_dict() for gap in self.gaps],
        }


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

    def backfill_range(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int,
        end_time: int,
        page_limit: int = 1000,
    ) -> BackfillResult:
        """Backfill the half-open UTC range [start_time, end_time)."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval: {interval}")
        if start_time < 0 or end_time <= start_time:
            raise ValueError("end_time must be greater than start_time")
        if page_limit < 1 or page_limit > 1500:
            raise ValueError("page_limit must be between 1 and 1500")

        symbol = symbol.upper()
        step = INTERVAL_MS[interval]
        cursor = start_time
        server_now = self.client.current_server_time_ms()
        pages = 0
        rows_received = 0
        inserted = 0
        existing = 0
        exchange_duplicates = 0
        seen_exchange: set[int] = set()

        while cursor < end_time:
            rows = self.client.klines(
                symbol,
                interval,
                start_time=cursor,
                end_time=end_time - 1,
                limit=page_limit,
            )
            pages += 1
            rows_received += len(rows)
            if not rows:
                break

            last_open: int | None = None
            max_open: int | None = None
            for row in rows:
                open_time = int(row[0])
                if last_open is not None and open_time < last_open:
                    raise ValueError("exchange returned out-of-order klines")
                last_open = open_time
                max_open = open_time if max_open is None else max(max_open, open_time)
                if open_time in seen_exchange:
                    exchange_duplicates += 1
                    continue
                seen_exchange.add(open_time)
                if open_time < start_time or open_time >= end_time:
                    continue
                candle = Candle.from_rest(symbol, interval, row)
                if candle.close_time >= server_now:
                    continue
                if self.journal.store_candle(candle.as_dict()):
                    inserted += 1
                else:
                    existing += 1

            if max_open is None or max_open < cursor:
                raise ValueError("exchange kline pagination did not advance")
            next_cursor = max_open + step
            if next_cursor <= cursor:
                raise ValueError("exchange kline pagination stalled")
            cursor = next_cursor
            if len(rows) < page_limit:
                break

        stored = {
            int(row["open_time"])
            for row in self.journal.candles(
                symbol, interval, start_time=start_time, end_time=end_time
            )
        }
        gaps: list[CandleGap] = []
        aligned_start = ((start_time + step - 1) // step) * step
        gap_start: int | None = None
        missing = 0
        for expected in range(aligned_start, end_time, step):
            if expected + step - 1 >= server_now:
                break
            if expected not in stored:
                if gap_start is None:
                    gap_start = expected
                missing += 1
            elif gap_start is not None:
                gaps.append(CandleGap(gap_start, expected, missing))
                gap_start = None
                missing = 0
        if gap_start is not None:
            gaps.append(CandleGap(gap_start, gap_start + missing * step, missing))

        return BackfillResult(
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            pages=pages,
            rows_received=rows_received,
            inserted=inserted,
            existing=existing,
            exchange_duplicates=exchange_duplicates,
            gaps=tuple(gaps),
        )

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
