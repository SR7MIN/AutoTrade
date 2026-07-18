import unittest
from decimal import Decimal

from autotrade.errors import RuleViolation
from autotrade.rules import SymbolRules, decimal_text, floor_to_increment


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "marginAsset": "USDT",
            "triggerProtect": "0.0500",
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "100.0",
                    "maxPrice": "1000000.0",
                    "tickSize": "0.10",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.001",
                    "maxQty": "1000",
                    "stepSize": "0.001",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": "0.001",
                    "maxQty": "120",
                    "stepSize": "0.001",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "50"},
            ],
        }
    ]
}


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = SymbolRules.from_exchange_info(EXCHANGE_INFO, "btcusdt")

    def test_floor_to_increment_is_decimal_safe(self) -> None:
        self.assertEqual(
            floor_to_increment(Decimal("1.2349"), Decimal("0.001")), Decimal("1.234")
        )

    def test_normalizes_price_and_quantity(self) -> None:
        self.assertEqual(self.rules.normalize_price(Decimal("65000.19")), Decimal("65000.10"))
        self.assertEqual(
            self.rules.normalize_quantity(Decimal("0.00199"), market=True), Decimal("0.001")
        )

    def test_rejects_min_notional(self) -> None:
        with self.assertRaises(RuleViolation):
            self.rules.validate(
                quantity=Decimal("0.001"),
                reference_price=Decimal("40000"),
                market=True,
            )

    def test_decimal_text_does_not_use_scientific_notation(self) -> None:
        self.assertEqual(decimal_text(Decimal("0.001000")), "0.001")


if __name__ == "__main__":
    unittest.main()

