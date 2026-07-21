import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from autotrade.errors import RuleViolation
from autotrade.intents import EntryIntent


class EntryIntentTests(unittest.TestCase):
    def test_intent_is_immutable_and_has_no_exchange_dependency(self) -> None:
        intent = EntryIntent.create(
            source="test-signal",
            symbol="btcusdt",
            side="buy",
            risk_usdt=Decimal("1"),
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
            leverage=3,
        )
        self.assertEqual(intent.symbol, "BTCUSDT")
        with self.assertRaises(FrozenInstanceError):
            intent.symbol = "ETHUSDT"

    def test_expired_intent_is_rejected(self) -> None:
        intent = EntryIntent.create(
            source="test", symbol="BTCUSDT", side="BUY", risk_usdt=Decimal("1"),
            stop_price=Decimal("49000"), take_profit_price=None, leverage=2,
        )
        with self.assertRaises(RuleViolation):
            intent.validate_freshness(intent.expires_at_ms + 1)

    def test_intent_round_trips_through_operator_queue_payload(self) -> None:
        intent = EntryIntent.create(
            source="test", symbol="BTCUSDT", side="SELL", risk_usdt=Decimal("2"),
            stop_price=Decimal("51000"), take_profit_price=None, leverage=2,
            min_stop_bps=Decimal("60"),
            max_stop_bps=Decimal("350"),
        )
        restored = EntryIntent.from_dict(intent.as_dict())
        self.assertEqual(restored, intent)


if __name__ == "__main__":
    unittest.main()
