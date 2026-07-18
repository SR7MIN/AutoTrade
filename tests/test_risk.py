import unittest
from decimal import Decimal

from autotrade.errors import RuleViolation
from autotrade.risk import build_position_plan
from autotrade.rules import SymbolRules
from tests.test_rules import EXCHANGE_INFO


class RiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = SymbolRules.from_exchange_info(EXCHANGE_INFO, "BTCUSDT")

    def test_sizes_from_stop_distance(self) -> None:
        plan = build_position_plan(
            rules=self.rules,
            side="BUY",
            entry_price=Decimal("50000"),
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
            risk_budget_usdt=Decimal("10"),
            leverage=5,
        )
        self.assertEqual(plan.quantity, Decimal("0.010"))
        self.assertEqual(plan.risk_usdt, Decimal("10.000"))

    def test_margin_cap_reduces_quantity(self) -> None:
        plan = build_position_plan(
            rules=self.rules,
            side="SELL",
            entry_price=Decimal("50000"),
            stop_price=Decimal("51000"),
            risk_budget_usdt=Decimal("100"),
            leverage=5,
            available_margin=Decimal("100"),
            margin_utilization=Decimal("0.50"),
        )
        self.assertEqual(plan.quantity, Decimal("0.005"))

    def test_rejects_stop_on_wrong_side(self) -> None:
        with self.assertRaises(RuleViolation):
            build_position_plan(
                rules=self.rules,
                side="BUY",
                entry_price=Decimal("50000"),
                stop_price=Decimal("51000"),
                risk_budget_usdt=Decimal("10"),
                leverage=3,
            )


if __name__ == "__main__":
    unittest.main()

