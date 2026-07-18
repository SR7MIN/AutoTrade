from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .errors import RuleViolation
from .rules import SymbolRules, decimal_text, decimal_value


@dataclass(frozen=True, slots=True)
class PositionPlan:
    symbol: str
    side: str
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    quantity: Decimal
    notional: Decimal
    risk_usdt: Decimal
    estimated_margin: Decimal
    leverage: int

    def as_dict(self) -> dict[str, str | int | None]:
        def money(value: Decimal) -> str:
            return decimal_text(value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))

        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": decimal_text(self.entry_price),
            "stop_price": decimal_text(self.stop_price),
            "take_profit_price": (
                decimal_text(self.take_profit_price) if self.take_profit_price is not None else None
            ),
            "quantity": decimal_text(self.quantity),
            "notional": money(self.notional),
            "risk_usdt": money(self.risk_usdt),
            "estimated_margin": money(self.estimated_margin),
            "leverage": self.leverage,
        }


def build_position_plan(
    *,
    rules: SymbolRules,
    side: str,
    entry_price: Decimal,
    stop_price: Decimal,
    risk_budget_usdt: Decimal,
    leverage: int,
    take_profit_price: Decimal | None = None,
    available_margin: Decimal | None = None,
    margin_utilization: Decimal = Decimal("0.50"),
    max_notional: Decimal | None = None,
    cost_bps: Decimal = Decimal("0"),
) -> PositionPlan:
    side = side.upper()
    entry_price = decimal_value(entry_price)
    stop_price = rules.normalize_price(decimal_value(stop_price))
    take_profit_price = (
        rules.normalize_price(decimal_value(take_profit_price))
        if take_profit_price is not None
        else None
    )
    risk_budget_usdt = decimal_value(risk_budget_usdt)
    if side not in {"BUY", "SELL"}:
        raise RuleViolation("side must be BUY or SELL")
    if leverage < 1 or leverage > 125:
        raise RuleViolation("leverage must be between 1 and 125")
    if entry_price <= 0 or stop_price <= 0 or risk_budget_usdt <= 0:
        raise RuleViolation("entry price, stop price and risk budget must be positive")
    if side == "BUY" and stop_price >= entry_price:
        raise RuleViolation("BUY stop price must be below entry price")
    if side == "SELL" and stop_price <= entry_price:
        raise RuleViolation("SELL stop price must be above entry price")
    if take_profit_price is not None:
        if side == "BUY" and take_profit_price <= entry_price:
            raise RuleViolation("BUY take-profit price must be above entry price")
        if side == "SELL" and take_profit_price >= entry_price:
            raise RuleViolation("SELL take-profit price must be below entry price")
    if not Decimal("0") < margin_utilization <= Decimal("1"):
        raise RuleViolation("margin utilization must be in (0, 1]")

    if cost_bps < 0:
        raise RuleViolation("cost basis points cannot be negative")
    stop_distance = abs(entry_price - stop_price) + entry_price * cost_bps / Decimal("10000")
    quantity = risk_budget_usdt / stop_distance
    if available_margin is not None:
        margin_cap_qty = (
            decimal_value(available_margin) * margin_utilization * Decimal(leverage) / entry_price
        )
        quantity = min(quantity, margin_cap_qty)
    if max_notional is not None:
        quantity = min(quantity, decimal_value(max_notional) / entry_price)
    quantity = rules.normalize_quantity(quantity, market=True)
    if quantity <= 0:
        raise RuleViolation("calculated quantity rounds down to zero")
    rules.validate(quantity=quantity, reference_price=entry_price, market=True)

    notional = quantity * entry_price
    actual_risk = quantity * stop_distance
    return PositionPlan(
        symbol=rules.symbol,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        quantity=quantity,
        notional=notional,
        risk_usdt=actual_risk,
        estimated_margin=notional / Decimal(leverage),
        leverage=leverage,
    )
