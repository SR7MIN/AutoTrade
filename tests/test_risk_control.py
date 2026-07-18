import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from autotrade.config import RiskSettings
from autotrade.errors import EntryPaused, RiskRejected
from autotrade.journal import OrderJournal
from autotrade.risk import PositionPlan
from autotrade.risk_control import RiskGovernor


def limits(**overrides):
    values = {
        "max_risk_usdt": Decimal("25"),
        "max_risk_fraction": Decimal("0.01"),
        "max_order_notional": Decimal("2500"),
        "max_symbol_notional": Decimal("2500"),
        "max_total_notional": Decimal("5000"),
        "max_leverage": 5,
        "max_open_symbols": 3,
        "max_daily_loss": Decimal("100"),
        "max_consecutive_losses": 3,
        "min_available_margin": Decimal("50"),
        "min_liquidation_distance": Decimal("0.10"),
        "fee_bps": Decimal("5"),
        "slippage_bps": Decimal("10"),
        "max_mark_age_seconds": 10,
    }
    values.update(overrides)
    return RiskSettings(**values)


def plan():
    return PositionPlan(
        symbol="BTCUSDT",
        side="BUY",
        entry_price=Decimal("50000"),
        stop_price=Decimal("49000"),
        take_profit_price=Decimal("52000"),
        quantity=Decimal("0.002"),
        notional=Decimal("100"),
        risk_usdt=Decimal("2"),
        estimated_margin=Decimal("33.33"),
        leverage=3,
    )


class RiskGovernorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(Path(self.directory.name) / "state.db")
        self.governor = RiskGovernor(limits(), self.journal)
        self.governor.unlock_entries("test setup")
        self.now = int(time.time() * 1000)
        self.account = {"totalWalletBalance": "1000", "availableBalance": "900"}

    def tearDown(self) -> None:
        self.journal.close()
        self.directory.cleanup()

    def test_approves_entry_within_all_limits(self) -> None:
        decision = self.governor.approve_entry(
            plan=plan(), requested_risk=Decimal("2"), account=self.account,
            positions=[], income=[], mark_time_ms=self.now, server_time_ms=self.now,
        )
        self.assertEqual(decision["approved_order_notional"], "100")

    def test_manual_pause_is_persistent(self) -> None:
        self.governor.lock_entries("maintenance")
        with self.assertRaises(EntryPaused):
            self.governor.precheck(requested_risk=Decimal("1"), leverage=2)
        self.governor.unlock_entries("operator reviewed account")
        self.governor.precheck(requested_risk=Decimal("1"), leverage=2)

    def test_daily_loss_locks_future_entries(self) -> None:
        with self.assertRaises(EntryPaused):
            self.governor.approve_entry(
                plan=plan(), requested_risk=Decimal("2"), account=self.account,
                positions=[],
                income=[
                    {
                        "incomeType": "REALIZED_PNL",
                        "income": "-101",
                        "time": self.now,
                        "tranId": 1,
                    }
                ],
                mark_time_ms=self.now,
                server_time_ms=self.now,
            )
        self.assertEqual(self.journal.get_control("entry_enabled"), "false")

    def test_rejects_stale_mark_price(self) -> None:
        with self.assertRaises(RiskRejected):
            self.governor.approve_entry(
                plan=plan(), requested_risk=Decimal("2"), account=self.account,
                positions=[], income=[], mark_time_ms=self.now - 11_000,
                server_time_ms=self.now,
            )

    def test_rejects_excess_leverage_before_exchange_mutation(self) -> None:
        with self.assertRaises(RiskRejected):
            self.governor.precheck(requested_risk=Decimal("1"), leverage=10)


if __name__ == "__main__":
    unittest.main()
