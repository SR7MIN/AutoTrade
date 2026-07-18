from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .errors import RuleViolation


ZERO = Decimal("0")


def decimal_value(value: str | int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= ZERO:
        raise ValueError("increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    status: str
    contract_type: str
    margin_asset: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    trigger_protect: Decimal

    @classmethod
    def from_exchange_info(cls, exchange_info: dict[str, Any], symbol: str) -> "SymbolRules":
        normalized = symbol.upper()
        symbol_data = next(
            (item for item in exchange_info.get("symbols", []) if item.get("symbol") == normalized),
            None,
        )
        if symbol_data is None:
            raise RuleViolation(f"Unknown symbol: {normalized}")
        filters = {item["filterType"]: item for item in symbol_data.get("filters", [])}
        try:
            price_filter = filters["PRICE_FILTER"]
            lot_filter = filters["LOT_SIZE"]
            market_filter = filters["MARKET_LOT_SIZE"]
            notional_filter = filters["MIN_NOTIONAL"]
        except KeyError as exc:
            raise RuleViolation(f"{normalized} is missing exchange filter {exc.args[0]}") from exc
        return cls(
            symbol=normalized,
            status=str(symbol_data.get("status", "")),
            contract_type=str(symbol_data.get("contractType", "")),
            margin_asset=str(symbol_data.get("marginAsset", "")),
            tick_size=decimal_value(price_filter["tickSize"]),
            min_price=decimal_value(price_filter["minPrice"]),
            max_price=decimal_value(price_filter["maxPrice"]),
            step_size=decimal_value(lot_filter["stepSize"]),
            min_qty=decimal_value(lot_filter["minQty"]),
            max_qty=decimal_value(lot_filter["maxQty"]),
            market_step_size=decimal_value(market_filter["stepSize"]),
            market_min_qty=decimal_value(market_filter["minQty"]),
            market_max_qty=decimal_value(market_filter["maxQty"]),
            min_notional=decimal_value(notional_filter["notional"]),
            trigger_protect=decimal_value(symbol_data.get("triggerProtect", "0")),
        )

    def ensure_tradeable(self) -> None:
        if self.status != "TRADING":
            raise RuleViolation(f"{self.symbol} status is {self.status}, not TRADING")
        if self.contract_type != "PERPETUAL":
            raise RuleViolation(f"{self.symbol} is not a perpetual contract")
        if self.margin_asset != "USDT":
            raise RuleViolation(f"{self.symbol} margin asset is not USDT")

    def normalize_price(self, price: Decimal) -> Decimal:
        return floor_to_increment(price, self.tick_size)

    def normalize_quantity(self, quantity: Decimal, *, market: bool) -> Decimal:
        step = self.market_step_size if market else self.step_size
        return floor_to_increment(quantity, step)

    def validate(
        self,
        *,
        quantity: Decimal,
        reference_price: Decimal,
        market: bool,
        price: Decimal | None = None,
    ) -> None:
        self.ensure_tradeable()
        step = self.market_step_size if market else self.step_size
        min_qty = self.market_min_qty if market else self.min_qty
        max_qty = self.market_max_qty if market else self.max_qty
        if quantity != floor_to_increment(quantity, step):
            raise RuleViolation(f"quantity {quantity} is not aligned to stepSize {step}")
        if quantity < min_qty or quantity > max_qty:
            raise RuleViolation(f"quantity {quantity} is outside [{min_qty}, {max_qty}]")
        if price is not None:
            if price != self.normalize_price(price):
                raise RuleViolation(f"price {price} is not aligned to tickSize {self.tick_size}")
            if price < self.min_price or price > self.max_price:
                raise RuleViolation(f"price {price} is outside [{self.min_price}, {self.max_price}]")
        notional = quantity * reference_price
        if notional < self.min_notional:
            raise RuleViolation(
                f"notional {decimal_text(notional)} is below minimum {self.min_notional}"
            )

