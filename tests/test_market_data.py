import tempfile
import unittest
from pathlib import Path

from autotrade.journal import OrderJournal
from autotrade.market_data import Candle, MarketDataService


class MarketClient:
    def current_server_time_ms(self):
        return 200_000

    def klines(self, symbol, interval, start_time=None, end_time=None, limit=500):
        return [
            [60_000, "1", "2", "0.5", "1.5", "10", 119_999, "0", 7],
            [120_000, "1.5", "3", "1", "2", "12", 179_999, "0", 9],
            [180_000, "2", "3", "1", "2.5", "8", 239_999, "0", 4],
        ]


class RangeMarketClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def current_server_time_ms(self):
        return 400_000

    def klines(self, symbol, interval, start_time=None, end_time=None, limit=500):
        self.calls.append((start_time, end_time, limit))
        values = [
            row for row in self.rows
            if (start_time is None or row[0] >= start_time)
            and (end_time is None or row[0] <= end_time)
        ]
        return values[:limit]


def rest_candle(open_time, close="100"):
    return [
        open_time, close, "101", "99", close, "10",
        open_time + 59_999, "0", 7,
    ]


class MarketDataTests(unittest.TestCase):
    def test_stream_candle_parsing_preserves_exchange_times(self) -> None:
        candle = Candle.from_stream(
            {
                "k": {
                    "s": "BTCUSDT", "i": "1m", "t": 1, "T": 2,
                    "o": "1", "h": "2", "l": "0.5", "c": "1.5",
                    "v": "10", "n": 3, "x": True,
                }
            }
        )
        self.assertTrue(candle.closed)
        self.assertEqual(candle.open_time, 1)

    def test_backfill_stores_only_closed_candles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = OrderJournal(Path(directory) / "state.db")
            try:
                service = MarketDataService(MarketClient(), journal, "wss://example")
                self.assertEqual(service.backfill("BTCUSDT", "1m"), 2)
                self.assertEqual(journal.latest_candle_open_time("BTCUSDT", "1m"), 120_000)
            finally:
                journal.close()

    def test_range_backfill_pages_reports_gaps_and_is_idempotent(self) -> None:
        rows = [rest_candle(value) for value in (0, 60_000, 180_000, 240_000)]
        client = RangeMarketClient(rows)
        with tempfile.TemporaryDirectory() as directory:
            journal = OrderJournal(Path(directory) / "research.db")
            try:
                service = MarketDataService(client, journal, "wss://example")
                result = service.backfill_range(
                    "btcusdt", "1m", start_time=0, end_time=300_000, page_limit=2
                )
                self.assertEqual(result.pages, 2)
                self.assertEqual(result.inserted, 4)
                self.assertEqual(result.gaps[0].start_time, 120_000)
                self.assertEqual(result.gaps[0].end_time, 180_000)
                self.assertEqual(result.as_dict()["gapCount"], 1)

                repeated = service.backfill_range(
                    "BTCUSDT", "1m", start_time=0, end_time=300_000, page_limit=2
                )
                self.assertEqual(repeated.inserted, 0)
                self.assertEqual(repeated.existing, 4)
                self.assertEqual(len(journal.candles("BTCUSDT", "1m")), 4)
            finally:
                journal.close()

    def test_range_backfill_rejects_out_of_order_exchange_rows(self) -> None:
        client = RangeMarketClient([rest_candle(60_000), rest_candle(0)])
        with tempfile.TemporaryDirectory() as directory:
            journal = OrderJournal(Path(directory) / "research.db")
            try:
                with self.assertRaisesRegex(ValueError, "out-of-order"):
                    MarketDataService(client, journal, "wss://example").backfill_range(
                        "BTCUSDT", "1m", start_time=0, end_time=120_000
                    )
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
