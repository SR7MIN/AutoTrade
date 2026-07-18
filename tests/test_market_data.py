import tempfile
import unittest
from pathlib import Path

from autotrade.journal import OrderJournal
from autotrade.market_data import Candle, MarketDataService


class MarketClient:
    def current_server_time_ms(self):
        return 200_000

    def klines(self, symbol, interval, start_time=None, limit=500):
        return [
            [60_000, "1", "2", "0.5", "1.5", "10", 119_999, "0", 7],
            [120_000, "1.5", "3", "1", "2", "12", 179_999, "0", 9],
            [180_000, "2", "3", "1", "2.5", "8", 239_999, "0", 4],
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


if __name__ == "__main__":
    unittest.main()
