from __future__ import annotations

import time
import uuid
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .binance_rest import BinanceRestClient
from .errors import BinanceAPIError, ProtectionError, RuleViolation
from .journal import OrderJournal
from .intents import EntryIntent
from .risk import PositionPlan, build_position_plan
from .risk_control import RiskGovernor, utc_day_start_ms
from .rules import SymbolRules, decimal_text, decimal_value


MAX_CLIENT_ID_LENGTH = 35


def client_id(kind: str) -> str:
    timestamp = int(time.time() * 1000)
    suffix = uuid.uuid4().hex[:8]
    fixed = f"at--{timestamp}-{suffix}"
    kind_budget = MAX_CLIENT_ID_LENGTH - len(fixed)
    safe_kind = re.sub(r"[^A-Za-z0-9_-]", "", kind)[:kind_budget] or "o"
    identifier = f"at-{safe_kind}-{timestamp}-{suffix}"
    if len(identifier) > MAX_CLIENT_ID_LENGTH:
        raise ValueError("generated client order id is too long")
    return identifier


@dataclass(frozen=True, slots=True)
class BracketResult:
    intent_id: int
    plan: PositionPlan
    entry: dict[str, Any]
    stop: dict[str, Any]
    take_profit: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "plan": self.plan.as_dict(),
            "entry": self.entry,
            "stop": self.stop,
            "take_profit": self.take_profit,
        }


class TradingService:
    def __init__(
        self,
        client: BinanceRestClient,
        journal: OrderJournal,
        risk_governor: RiskGovernor | None = None,
    ) -> None:
        self.client = client
        self.journal = journal
        self.risk_governor = risk_governor
        self._rules_cache: dict[str, tuple[float, SymbolRules]] = {}

    def load_rules(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
        risk_reducing: bool = False,
    ) -> SymbolRules:
        symbol = symbol.upper()
        cached = self._rules_cache.get(symbol)
        if not force_refresh and cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        rules = SymbolRules.from_exchange_info(
            self.client.exchange_info(risk_reducing=risk_reducing), symbol
        )
        self._rules_cache[symbol] = (time.monotonic(), rules)
        return rules

    def preview(
        self,
        *,
        symbol: str,
        side: str,
        risk_usdt: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal | None,
        leverage: int,
        available_margin: Decimal | None = None,
        margin_utilization: Decimal = Decimal("0.50"),
        max_notional: Decimal | None = None,
    ) -> PositionPlan:
        rules = self.load_rules(symbol)
        mark = decimal_value(self.client.mark_price(rules.symbol)["markPrice"])
        return build_position_plan(
            rules=rules,
            side=side,
            entry_price=mark,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            risk_budget_usdt=risk_usdt,
            leverage=leverage,
            available_margin=available_margin,
            margin_utilization=margin_utilization,
            max_notional=max_notional,
            cost_bps=(
                self.risk_governor.limits.fee_bps
                + self.risk_governor.limits.slippage_bps
                if self.risk_governor
                else Decimal("0")
            ),
        )

    def _assert_clean_symbol(self, symbol: str) -> None:
        mode = self.client.position_mode()
        if bool(mode.get("dualSidePosition")):
            raise RuleViolation("This MVP supports One-way Mode only; Hedge Mode is enabled")
        positions = self.client.positions(symbol)
        if any(decimal_value(position.get("positionAmt", "0")) != 0 for position in positions):
            raise RuleViolation(f"{symbol} already has an open position")
        ordinary = self.client.open_orders(symbol)
        algo = self.client.open_algo_orders(symbol)
        if ordinary or algo:
            raise RuleViolation(f"{symbol} already has open ordinary or algo orders")

    def place_market_bracket(
        self,
        *,
        symbol: str,
        side: str,
        risk_usdt: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal | None,
        leverage: int,
        margin_utilization: Decimal = Decimal("0.50"),
        min_stop_bps: Decimal | None = None,
        max_stop_bps: Decimal | None = None,
    ) -> BracketResult:
        symbol = symbol.upper()
        side = side.upper()
        if self.risk_governor:
            self.risk_governor.precheck(requested_risk=risk_usdt, leverage=leverage)
        self.client.sync_time()
        self._assert_clean_symbol(symbol)

        try:
            self.client.change_margin_type(symbol, "ISOLATED")
        except BinanceAPIError as exc:
            if exc.code != -4046:  # No need to change margin type.
                raise
        leverage_result = self.client.change_leverage(symbol, leverage)
        max_notional_value = leverage_result.get("maxNotionalValue")
        account = self.client.account()
        positions = self.client.positions()
        available_margin = decimal_value(account["availableBalance"])
        plan = self.preview(
            symbol=symbol,
            side=side,
            risk_usdt=risk_usdt,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            leverage=leverage,
            available_margin=available_margin,
            margin_utilization=margin_utilization,
            max_notional=(
                decimal_value(max_notional_value) if max_notional_value is not None else None
            ),
        )
        if min_stop_bps is not None:
            actual_stop_bps = (
                abs(plan.entry_price - plan.stop_price)
                / plan.entry_price
                * Decimal("10000")
            )
            if actual_stop_bps < Decimal(min_stop_bps):
                raise RuleViolation(
                    "actual entry-to-stop distance is below the strategy minimum"
                )
        if max_stop_bps is not None:
            actual_stop_bps = (
                abs(plan.entry_price - plan.stop_price)
                / plan.entry_price
                * Decimal("10000")
            )
            if actual_stop_bps > Decimal(max_stop_bps):
                raise RuleViolation(
                    "actual entry-to-stop distance exceeds the strategy maximum"
                )
        self.journal.record_account_snapshot(account)
        if self.risk_governor:
            mark = self.client.mark_price(symbol)
            income = self.client.income_history(start_time=utc_day_start_ms())
            self.risk_governor.approve_entry(
                plan=plan,
                requested_risk=risk_usdt,
                account=account,
                positions=positions,
                income=income,
                mark_time_ms=int(mark["time"]) if mark.get("time") is not None else None,
                server_time_ms=self.client.current_server_time_ms(),
            )

        entry_client_id = client_id("entry")
        intent_id = self.journal.create_intent(
            client_order_id=entry_client_id,
            symbol=plan.symbol,
            side=plan.side,
            quantity=decimal_text(plan.quantity),
            stop_price=decimal_text(plan.stop_price),
            take_profit_price=(
                decimal_text(plan.take_profit_price)
                if plan.take_profit_price is not None
                else None
            ),
            details={"plan": plan.as_dict(), "environment": self.client.settings.environment},
        )
        self.journal.update(intent_id, "SUBMITTING")
        self.journal.record_order(
            {
                "symbol": plan.symbol,
                "clientOrderId": entry_client_id,
                "side": plan.side,
                "type": "MARKET",
                "status": "SUBMITTING",
                "origQty": decimal_text(plan.quantity),
                "reduceOnly": False,
            },
            family="ORDINARY",
            role="ENTRY",
            intent_id=intent_id,
        )

        try:
            entry = self.client.new_order(
                symbol=plan.symbol,
                side=plan.side,
                type="MARKET",
                quantity=decimal_text(plan.quantity),
                newClientOrderId=entry_client_id,
                newOrderRespType="RESULT",
            )
        except BinanceAPIError as exc:
            if not exc.execution_unknown:
                self.journal.update(intent_id, "REJECTED", details={"error": str(exc)})
                if exc.status_code == 418 and self.risk_governor:
                    self.risk_governor.lock_entries("Binance IP rate-limit ban (HTTP 418)")
                raise
            entry = self._reconcile_unknown_entry(plan.symbol, entry_client_id, intent_id, exc)
        except httpx.HTTPError as exc:
            entry = self._reconcile_unknown_entry(plan.symbol, entry_client_id, intent_id, exc)

        entry.setdefault("clientOrderId", entry_client_id)
        entry.setdefault("symbol", plan.symbol)
        entry.setdefault("side", plan.side)
        entry.setdefault("type", "MARKET")
        executed_qty = decimal_value(entry.get("executedQty", "0"))
        self.journal.record_order(entry, family="ORDINARY", role="ENTRY", intent_id=intent_id)
        if executed_qty <= 0:
            self.journal.update(
                intent_id,
                "UNKNOWN",
                entry_order_id=entry.get("orderId"),
                details={"entry": entry, "reason": "entry has no executed quantity"},
            )
            raise ProtectionError("Entry execution has no confirmed filled quantity; reconcile manually")
        self.journal.update(intent_id, "ENTRY_FILLED", entry_order_id=entry.get("orderId"))

        exit_side = "SELL" if plan.side == "BUY" else "BUY"
        try:
            stop = self.client.new_algo_order(
                algoType="CONDITIONAL",
                symbol=plan.symbol,
                side=exit_side,
                type="STOP_MARKET",
                quantity=decimal_text(executed_qty),
                triggerPrice=decimal_text(plan.stop_price),
                workingType="MARK_PRICE",
                reduceOnly=True,
                clientAlgoId=client_id("stop"),
                newOrderRespType="ACK",
            )
            self.journal.record_order(stop, family="ALGO", role="STOP", intent_id=intent_id)
        except Exception as stop_error:
            emergency = self._emergency_close(plan.symbol, exit_side, executed_qty)
            self.journal.update(
                intent_id,
                "EMERGENCY_CLOSED",
                details={"stop_error": str(stop_error), "emergency_close": emergency},
            )
            raise ProtectionError(
                f"Stop order failed; emergency close submitted: {stop_error}"
            ) from stop_error

        take_profit: dict[str, Any] | None = None
        if plan.take_profit_price is not None:
            try:
                take_profit = self.client.new_algo_order(
                    algoType="CONDITIONAL",
                    symbol=plan.symbol,
                    side=exit_side,
                    type="TAKE_PROFIT_MARKET",
                    quantity=decimal_text(executed_qty),
                    triggerPrice=decimal_text(plan.take_profit_price),
                    workingType="MARK_PRICE",
                    reduceOnly=True,
                    clientAlgoId=client_id("take-profit"),
                    newOrderRespType="ACK",
                )
                self.journal.record_order(
                    take_profit, family="ALGO", role="TAKE_PROFIT", intent_id=intent_id
                )
            except Exception as take_profit_error:
                self.journal.update(
                    intent_id,
                    "PROTECTED_WITHOUT_TAKE_PROFIT",
                    details={
                        "entry": entry,
                        "stop": stop,
                        "take_profit_error": str(take_profit_error),
                    },
                )
                raise ProtectionError(
                    f"Position has a stop but take-profit placement failed: {take_profit_error}"
                ) from take_profit_error

        self.journal.update(
            intent_id,
            "PROTECTED",
            details={"entry": entry, "stop": stop, "take_profit": take_profit},
        )
        return BracketResult(intent_id, plan, entry, stop, take_profit)

    def execute_intent(self, intent: EntryIntent) -> BracketResult:
        intent.validate_freshness(self.client.current_server_time_ms())
        self.journal.append_audit(
            "entry_intent",
            "ACCEPTED_FOR_EXECUTION",
            symbol=intent.symbol,
            correlation_id=intent.intent_id,
            payload={"source": intent.source},
        )
        return self.place_market_bracket(
            symbol=intent.symbol,
            side=intent.side,
            risk_usdt=intent.risk_usdt,
            stop_price=intent.stop_price,
            take_profit_price=intent.take_profit_price,
            leverage=intent.leverage,
            margin_utilization=intent.margin_utilization,
            min_stop_bps=intent.min_stop_bps,
            max_stop_bps=intent.max_stop_bps,
        )

    def _reconcile_unknown_entry(
        self,
        symbol: str,
        entry_client_id: str,
        intent_id: int,
        original_error: Exception,
    ) -> dict[str, Any]:
        last_query_error: Exception | None = None
        for attempt in range(5):
            try:
                entry = self.client.query_order(symbol, client_order_id=entry_client_id)
                status = str(entry.get("status", ""))
                executed = decimal_value(entry.get("executedQty", "0"))
                if executed > 0 or status in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                    return entry
            except Exception as query_error:
                last_query_error = query_error
            if attempt < 4:
                time.sleep(0.2 * (2**attempt))
        self.journal.update(
            intent_id,
            "UNKNOWN",
            details={
                "submit_error": str(original_error),
                "query_error": str(last_query_error) if last_query_error else None,
            },
        )
        raise ProtectionError(
            "Entry result is unknown after bounded reconciliation; no retry was sent"
        ) from original_error

    def _emergency_close(
        self, symbol: str, exit_side: str, quantity: Decimal
    ) -> dict[str, Any]:
        try:
            emergency_client_id = client_id("emergency")
            response = self.client.new_order(
                symbol=symbol,
                side=exit_side,
                type="MARKET",
                quantity=decimal_text(quantity),
                reduceOnly=True,
                newClientOrderId=emergency_client_id,
                newOrderRespType="RESULT",
                _risk_reducing=True,
            )
            response.setdefault("clientOrderId", emergency_client_id)
            response.setdefault("symbol", symbol)
            response.setdefault("side", exit_side)
            response.setdefault("type", "MARKET")
            self.journal.record_order(response, family="ORDINARY", role="EMERGENCY_CLOSE")
            return response
        except Exception as close_error:
            raise ProtectionError(
                f"CRITICAL: stop placement and emergency close both failed: {close_error}"
            ) from close_error

    def cancel_all(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        self.client.sync_time(risk_reducing=True)
        ordinary_error: str | None = None
        algo_error: str | None = None
        ordinary: dict[str, Any] | None = None
        algo: dict[str, Any] | None = None
        try:
            ordinary = self.client.cancel_all_orders(symbol)
        except Exception as exc:
            ordinary_error = str(exc)
        try:
            algo = self.client.cancel_all_algo_orders(symbol)
        except Exception as exc:
            algo_error = str(exc)
        result = {
            "symbol": symbol,
            "ordinary": ordinary,
            "algo": algo,
            "ordinary_error": ordinary_error,
            "algo_error": algo_error,
        }
        if ordinary_error or algo_error:
            raise ProtectionError(f"One or more cancel operations failed: {result}")
        return result

    def take_profit_parameters(
        self, symbol: str, trigger_price: Decimal
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        mode = self.client.position_mode()
        if bool(mode.get("dualSidePosition")):
            raise RuleViolation("This MVP supports One-way Mode only; Hedge Mode is enabled")

        active_positions = [
            position
            for position in self.client.positions(symbol)
            if decimal_value(position.get("positionAmt", "0")) != 0
        ]
        if len(active_positions) != 1:
            raise RuleViolation(f"Expected exactly one active {symbol} position")
        position_amount = decimal_value(active_positions[0]["positionAmt"])
        quantity = abs(position_amount)

        existing_algo = self.client.open_algo_orders(symbol)
        if any(
            order.get("orderType", order.get("type")) == "TAKE_PROFIT_MARKET"
            for order in existing_algo
        ):
            raise RuleViolation(f"{symbol} already has a TAKE_PROFIT_MARKET order")

        rules = self.load_rules(symbol)
        mark_price = decimal_value(self.client.mark_price(symbol)["markPrice"])
        normalized_trigger = rules.normalize_price(decimal_value(trigger_price))
        if position_amount > 0 and normalized_trigger <= mark_price:
            raise RuleViolation("Long-position take-profit must be above the current mark price")
        if position_amount < 0 and normalized_trigger >= mark_price:
            raise RuleViolation("Short-position take-profit must be below the current mark price")
        rules.validate(quantity=quantity, reference_price=mark_price, market=True)

        return {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": "SELL" if position_amount > 0 else "BUY",
            "type": "TAKE_PROFIT_MARKET",
            "quantity": decimal_text(quantity),
            "triggerPrice": decimal_text(normalized_trigger),
            "workingType": "MARK_PRICE",
            "reduceOnly": True,
            "clientAlgoId": client_id("take-profit"),
            "newOrderRespType": "ACK",
        }

    def place_take_profit(self, symbol: str, trigger_price: Decimal) -> dict[str, Any]:
        self.client.sync_time()
        response = self.client.new_algo_order(
            **self.take_profit_parameters(symbol, trigger_price)
        )
        self.journal.record_order(response, family="ALGO", role="TAKE_PROFIT")
        return response

    def _active_position(self, symbol: str) -> tuple[dict[str, Any], Decimal]:
        symbol = symbol.upper()
        mode = self.client.position_mode(risk_reducing=True)
        if bool(mode.get("dualSidePosition")):
            raise RuleViolation("This system supports One-way Mode only")
        active = [
            position
            for position in self.client.positions(symbol, risk_reducing=True)
            if decimal_value(position.get("positionAmt", "0")) != 0
        ]
        if len(active) != 1:
            raise RuleViolation(f"Expected exactly one active {symbol} position")
        return active[0], decimal_value(active[0]["positionAmt"])

    def close_position_parameters(
        self, symbol: str, quantity: Decimal | None = None
    ) -> dict[str, Any]:
        position, amount = self._active_position(symbol)
        requested = abs(amount) if quantity is None else decimal_value(quantity)
        normalized = requested
        if quantity is not None:
            rules = self.load_rules(symbol, risk_reducing=True)
            normalized = rules.normalize_quantity(requested, market=True)
        if normalized <= 0 or normalized != requested:
            raise RuleViolation("close quantity is not aligned to MARKET_LOT_SIZE")
        if normalized > abs(amount):
            raise RuleViolation("close quantity exceeds the current position")
        return {
            "symbol": str(position["symbol"]),
            "side": "SELL" if amount > 0 else "BUY",
            "type": "MARKET",
            "quantity": decimal_text(normalized),
            "reduceOnly": True,
            "newClientOrderId": client_id("close"),
            "newOrderRespType": "RESULT",
        }

    def close_position(
        self, symbol: str, quantity: Decimal | None = None
    ) -> dict[str, Any]:
        self.client.sync_time(risk_reducing=True)
        parameters = self.close_position_parameters(symbol, quantity)
        self.journal.record_order(
            {
                **parameters,
                "clientOrderId": parameters["newClientOrderId"],
                "status": "SUBMITTING",
                "origQty": parameters["quantity"],
            },
            family="ORDINARY",
            role="CLOSE",
        )
        response = self.client.new_order(**parameters, _risk_reducing=True)
        self.journal.record_order(response, family="ORDINARY", role="CLOSE")
        remaining = [
            item
            for item in self.client.positions(symbol.upper(), risk_reducing=True)
            if decimal_value(item.get("positionAmt", "0")) != 0
        ]
        if not remaining:
            cleanup = self.cancel_all(symbol)
        else:
            cleanup = self.sync_protection_quantities(symbol)
        return {"close": response, "postClose": cleanup}

    def protection_parameters(
        self,
        symbol: str,
        order_type: str,
        trigger_price: Decimal,
    ) -> dict[str, Any]:
        _, amount = self._active_position(symbol)
        rules = self.load_rules(symbol, risk_reducing=True)
        mark = decimal_value(
            self.client.mark_price(symbol.upper(), risk_reducing=True)["markPrice"]
        )
        trigger = rules.normalize_price(decimal_value(trigger_price))
        order_type = order_type.upper()
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise RuleViolation("protection type must be STOP_MARKET or TAKE_PROFIT_MARKET")
        is_long = amount > 0
        if order_type == "STOP_MARKET":
            valid = trigger < mark if is_long else trigger > mark
            role = "STOP"
        else:
            valid = trigger > mark if is_long else trigger < mark
            role = "TAKE_PROFIT"
        if not valid:
            raise RuleViolation(f"{role} trigger is on the wrong side of mark price {mark}")
        return {
            "algoType": "CONDITIONAL",
            "symbol": symbol.upper(),
            "side": "SELL" if is_long else "BUY",
            "type": order_type,
            "quantity": decimal_text(abs(amount)),
            "triggerPrice": decimal_text(trigger),
            "workingType": "MARK_PRICE",
            "reduceOnly": True,
            "clientAlgoId": client_id(role.lower()),
            "newOrderRespType": "ACK",
        }

    def replace_protection(
        self, symbol: str, order_type: str, trigger_price: Decimal
    ) -> dict[str, Any]:
        self.client.sync_time(risk_reducing=True)
        order_type = order_type.upper()
        role = "STOP" if order_type == "STOP_MARKET" else "TAKE_PROFIT"
        existing = [
            order
            for order in self.client.open_algo_orders(symbol.upper(), risk_reducing=True)
            if order.get("orderType", order.get("type")) == order_type
        ]
        parameters = self.protection_parameters(symbol, order_type, trigger_price)
        replacement = self.client.new_algo_order(**parameters)
        self.journal.record_order(replacement, family="ALGO", role=role)
        canceled: list[dict[str, Any]] = []
        for order in existing:
            result = self.client.cancel_algo_order(
                algo_id=order.get("algoId"), client_algo_id=order.get("clientAlgoId")
            )
            canceled.append(result)
            old_client_id = order.get("clientAlgoId")
            if old_client_id:
                self.journal.transition_order(
                    str(old_client_id),
                    "CANCELED",
                    event_type="REPLACED",
                    exchange_time=result.get("updateTime"),
                    payload=result,
                )
        return {"replacement": replacement, "canceled": canceled}

    def protect_position(
        self,
        symbol: str,
        *,
        stop_price: Decimal,
        take_profit_price: Decimal | None = None,
    ) -> dict[str, Any]:
        self.client.sync_time(risk_reducing=True)
        results = {
            "stop": self.replace_protection(symbol, "STOP_MARKET", stop_price),
            "takeProfit": None,
        }
        if take_profit_price is not None:
            results["takeProfit"] = self.replace_protection(
                symbol, "TAKE_PROFIT_MARKET", take_profit_price
            )
        return results

    def sync_protection_quantities(self, symbol: str) -> dict[str, Any]:
        positions = [
            item
            for item in self.client.positions(symbol.upper(), risk_reducing=True)
            if decimal_value(item.get("positionAmt", "0")) != 0
        ]
        if not positions:
            return self.cancel_all(symbol)
        target = abs(decimal_value(positions[0]["positionAmt"]))
        replaced: list[dict[str, Any]] = []
        for order in self.client.open_algo_orders(symbol.upper(), risk_reducing=True):
            order_type = order.get("orderType", order.get("type"))
            if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
                continue
            quantity = decimal_value(order.get("quantity", order.get("origQty", "0")))
            if quantity == target:
                continue
            trigger = decimal_value(order.get("triggerPrice", order.get("stopPrice")))
            replaced.append(self.replace_protection(symbol, str(order_type), trigger))
        deduplicated = self.deduplicate_protection(symbol)
        return {
            "symbol": symbol.upper(),
            "targetQuantity": str(target),
            "replaced": replaced,
            "deduplicated": deduplicated,
        }

    def deduplicate_protection(self, symbol: str) -> dict[str, Any]:
        canceled: list[dict[str, Any]] = []
        orders = self.client.open_algo_orders(symbol.upper(), risk_reducing=True)
        for order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            matching = [
                order
                for order in orders
                if order.get("orderType", order.get("type")) == order_type
            ]
            matching.sort(
                key=lambda order: int(
                    order.get("updateTime") or order.get("createTime") or order.get("algoId") or 0
                ),
                reverse=True,
            )
            for duplicate in matching[1:]:
                result = self.client.cancel_algo_order(
                    algo_id=duplicate.get("algoId"),
                    client_algo_id=duplicate.get("clientAlgoId"),
                )
                canceled.append(result)
        return {"canceled": canceled}
