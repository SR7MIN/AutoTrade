from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import RiskSettings
from .errors import EntryPaused, RiskRejected
from .journal import OrderJournal
from .risk import PositionPlan
from .rules import decimal_value


RISK_INCOME_TYPES = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}


def utc_day_start_ms() -> int:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000)


class RiskGovernor:
    """Account-level hard limits that cannot be bypassed by an order producer."""

    def __init__(self, limits: RiskSettings, journal: OrderJournal) -> None:
        self.limits = limits
        self.journal = journal

    def precheck(self, *, requested_risk: Decimal, leverage: int) -> None:
        controls = self.journal.control_snapshot()
        if self.journal.get_control("entry_enabled", "false") != "true":
            reason = controls.get("entry_enabled", {}).get("reason")
            raise EntryPaused(f"New entries are paused: {reason or 'no reason recorded'}")
        unhealthy = [
            key
            for key, state in controls.items()
            if key.endswith("_healthy") and state.get("value") != "true"
        ]
        if unhealthy:
            raise EntryPaused(f"runtime data channels are unhealthy: {', '.join(unhealthy)}")
        if requested_risk > self.limits.max_risk_usdt:
            raise RiskRejected(
                f"requested risk {requested_risk} exceeds {self.limits.max_risk_usdt} USDT"
            )
        if leverage > self.limits.max_leverage:
            raise RiskRejected(
                f"leverage {leverage} exceeds configured maximum {self.limits.max_leverage}"
            )

    def approve_entry(
        self,
        *,
        plan: PositionPlan,
        requested_risk: Decimal,
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        income: list[dict[str, Any]],
        mark_time_ms: int | None,
        server_time_ms: int,
    ) -> dict[str, Any]:
        self.precheck(requested_risk=requested_risk, leverage=plan.leverage)
        wallet = decimal_value(account.get("totalWalletBalance", "0"))
        available = decimal_value(account.get("availableBalance", "0"))
        if account.get("canTrade") is False:
            raise RiskRejected("Binance account reports canTrade=false")
        if wallet <= 0:
            raise RiskRejected("wallet balance is not positive")
        if requested_risk > wallet * self.limits.max_risk_fraction:
            raise RiskRejected(
                f"requested risk exceeds {self.limits.max_risk_fraction} of wallet balance"
            )
        if available < self.limits.min_available_margin:
            raise RiskRejected(
                f"available margin {available} is below {self.limits.min_available_margin}"
            )
        if plan.notional > self.limits.max_order_notional:
            raise RiskRejected(
                f"order notional {plan.notional} exceeds {self.limits.max_order_notional}"
            )

        active = [
            position
            for position in positions
            if decimal_value(position.get("positionAmt", "0")) != 0
        ]
        active_symbols = {str(position.get("symbol")) for position in active}
        total_notional = sum(
            (abs(decimal_value(position.get("notional", "0"))) for position in active),
            Decimal("0"),
        )
        symbol_notional = sum(
            (
                abs(decimal_value(position.get("notional", "0")))
                for position in active
                if position.get("symbol") == plan.symbol
            ),
            Decimal("0"),
        )
        if plan.symbol not in active_symbols and len(active_symbols) >= self.limits.max_open_symbols:
            raise RiskRejected("maximum number of open symbols has been reached")
        if symbol_notional + plan.notional > self.limits.max_symbol_notional:
            raise RiskRejected("symbol notional limit would be exceeded")
        if total_notional + plan.notional > self.limits.max_total_notional:
            raise RiskRejected("total account notional limit would be exceeded")

        day_income = [
            item
            for item in income
            if item.get("incomeType") in RISK_INCOME_TYPES
            and int(item.get("time", 0)) >= utc_day_start_ms()
        ]
        daily_pnl = sum(
            (decimal_value(item.get("income", "0")) for item in day_income), Decimal("0")
        )
        if daily_pnl <= -self.limits.max_daily_loss:
            self.lock_entries(f"daily loss limit reached: {daily_pnl}")
            raise EntryPaused(f"daily loss limit reached: {daily_pnl}")

        consecutive_losses = 0
        realized_by_transaction: dict[str, tuple[int, Decimal]] = {}
        for index, item in enumerate(day_income):
            if item.get("incomeType") != "REALIZED_PNL":
                continue
            transaction = str(item.get("tranId") or f"row-{index}")
            event_time = int(item.get("time", 0))
            previous_time, previous_value = realized_by_transaction.get(
                transaction, (event_time, Decimal("0"))
            )
            realized_by_transaction[transaction] = (
                max(previous_time, event_time),
                previous_value + decimal_value(item.get("income", "0")),
            )
        realized = [
            value
            for _, value in sorted(
                realized_by_transaction.values(), key=lambda item: item[0], reverse=True
            )
            if value != 0
        ]
        for value in realized:
            if value < 0:
                consecutive_losses += 1
            else:
                break
        if consecutive_losses >= self.limits.max_consecutive_losses:
            self.lock_entries(f"consecutive loss limit reached: {consecutive_losses}")
            raise EntryPaused("consecutive loss limit reached")

        if mark_time_ms is None or server_time_ms - mark_time_ms > (
            self.limits.max_mark_age_seconds * 1000
        ):
            raise RiskRejected("mark price is stale or has no exchange timestamp")

        decision = {
            "wallet_balance": str(wallet),
            "available_margin": str(available),
            "daily_pnl": str(daily_pnl),
            "consecutive_losses": consecutive_losses,
            "existing_total_notional": str(total_notional),
            "approved_order_notional": str(plan.notional),
        }
        self.journal.append_audit(
            "risk", "ENTRY_APPROVED", symbol=plan.symbol, payload=decision
        )
        return decision

    def evaluate_open_positions(
        self, positions: list[dict[str, Any]], server_prices: dict[str, Decimal]
    ) -> list[dict[str, str]]:
        breaches: list[dict[str, str]] = []
        for position in positions:
            amount = decimal_value(position.get("positionAmt", "0"))
            if amount == 0:
                continue
            symbol = str(position.get("symbol"))
            mark = server_prices.get(symbol)
            liquidation = decimal_value(position.get("liquidationPrice", "0"))
            if mark and liquidation > 0:
                distance = abs(mark - liquidation) / mark
                if distance < self.limits.min_liquidation_distance:
                    breaches.append(
                        {
                            "symbol": symbol,
                            "type": "LIQUIDATION_DISTANCE",
                            "value": str(distance),
                        }
                    )
        return breaches

    def lock_entries(self, reason: str) -> None:
        self.journal.set_control("entry_enabled", "false", reason)

    def unlock_entries(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("an explicit unlock reason is required")
        self.journal.set_control("entry_enabled", "true", reason)
