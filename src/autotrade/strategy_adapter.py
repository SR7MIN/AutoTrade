from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import ConfigurationError, EntryPaused, RuleViolation
from .intents import EntryIntent
from .journal import OrderJournal
from .locking import lock_owner_active
from .risk_control import RiskGovernor
from .strategy import StrategyDecision, StrategyExitDecision, StrategySignal
from .strategy_manager import StrategyInstanceConfig


@dataclass(frozen=True, slots=True)
class StrategySubmission:
    mode: str
    signal_id: str
    intent: EntryIntent | None
    command_id: int | None
    action: str = "ENTER"
    decision_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "signalId": self.signal_id,
            "commandId": self.command_id,
            "action": self.action,
            "decisionId": self.decision_id,
            "intent": self.intent.as_dict() if self.intent is not None else None,
        }


class TestnetStrategyAdapter:
    """Strict bridge from reviewed strategy signals to the daemon command queue."""

    max_risk_usdt = 1
    max_leverage = 3

    def __init__(
        self,
        settings: Settings,
        journal: OrderJournal,
        instance: StrategyInstanceConfig,
        implementation_version: str,
        testnet_only: bool = False,
        research_only: bool = False,
    ) -> None:
        self.settings = settings
        self.journal = journal
        self.instance = instance
        self.implementation_version = implementation_version
        self.testnet_only = testnet_only
        self.research_only = research_only

    def submit(
        self,
        signal: StrategySignal,
        *,
        execute: bool,
        now_ms: int | None = None,
        max_signal_age_seconds: int = 90,
    ) -> StrategySubmission:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        signal_id = self.signal_id(signal)
        self._validate_signal(signal, now_ms, max_signal_age_seconds)
        RiskGovernor(self.settings.risk, self.journal).precheck(
            requested_risk=signal.risk_usdt, leverage=signal.leverage
        )
        self._validate_local_state(signal, signal_id, execute=execute)
        intent = EntryIntent.create(
            source=f"strategy:{signal.strategy}:{signal.version}:{signal_id}",
            symbol=signal.symbol,
            side=signal.side,
            risk_usdt=signal.risk_usdt,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            leverage=signal.leverage,
            margin_utilization=signal.margin_utilization,
            min_stop_bps=self._minimum_stop_bps(signal),
            max_stop_bps=self._maximum_stop_bps(signal),
            ttl_seconds=30,
        )
        if not execute:
            return StrategySubmission("preview", signal_id, intent, None)
        command_id = self.journal.enqueue_strategy_signal(signal_id, intent.as_dict())
        if command_id is None:
            raise RuleViolation("strategy signal has already been submitted")
        self.journal.append_audit(
            "strategy",
            "SIGNAL_QUEUED",
            symbol=signal.symbol,
            correlation_id=signal_id,
            payload={
                "strategy": signal.strategy,
                "version": signal.version,
                "candle_close_time": signal.candle_close_time,
                "command_id": command_id,
            },
        )
        return StrategySubmission("queued", signal_id, intent, command_id)

    def submit_decision(
        self,
        decision: StrategyDecision | StrategyExitDecision,
        *,
        execute: bool,
        now_ms: int | None = None,
        max_signal_age_seconds: int = 90,
    ) -> StrategySubmission:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(decision, StrategyExitDecision):
            return self._submit_exit_decision(
                decision,
                execute=execute,
                now_ms=now_ms,
                max_signal_age_seconds=max_signal_age_seconds,
            )
        signal = decision.entry_signal
        self._validate_signal(signal, now_ms, max_signal_age_seconds)
        if (
            decision.strategy != signal.strategy
            or decision.version != signal.version
            or decision.instance_id != signal.instance_id
            or decision.symbol != signal.symbol
            or decision.interval != signal.interval
            or decision.candle_close_time != signal.candle_close_time
        ):
            raise RuleViolation("strategy decision does not match its entry signal")
        RiskGovernor(self.settings.risk, self.journal).precheck(
            requested_risk=signal.risk_usdt, leverage=signal.leverage
        )
        self._validate_local_health(signal, execute=execute)
        decision_id = decision.decision_id
        if self.journal.strategy_signal_command_exists(decision_id):
            raise RuleViolation("strategy decision has already been submitted")
        if self.journal.pending_entry_command_exists():
            raise RuleViolation("another strategy entry or reversal is already pending")
        active_intent = self.journal.latest_active_intent(signal.symbol)
        active_orders = self.journal.active_orders(signal.symbol)
        if decision.action == "ENTER":
            if active_intent or active_orders:
                raise RuleViolation(f"{signal.symbol} is not locally flat")
        else:
            if active_intent is None:
                raise RuleViolation("strategy reversal requires an active local intent")
            expected_side = "BUY" if decision.current_position == "LONG" else "SELL"
            if str(active_intent.get("side")) != expected_side:
                raise RuleViolation("strategy reversal does not match the active local side")
            if not active_orders:
                raise RuleViolation("strategy reversal requires active protection orders")
        intent = EntryIntent.create(
            source=(
                f"strategy:{decision.strategy}:{decision.version}:"
                f"{decision_id}:{decision.action.lower()}"
            ),
            symbol=signal.symbol,
            side=signal.side,
            risk_usdt=signal.risk_usdt,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            leverage=signal.leverage,
            margin_utilization=signal.margin_utilization,
            min_stop_bps=self._minimum_stop_bps(signal),
            max_stop_bps=self._maximum_stop_bps(signal),
            ttl_seconds=30,
        )
        if not execute:
            return StrategySubmission(
                "preview",
                signal.signal_id,
                intent,
                None,
                action=decision.action,
                decision_id=decision_id,
            )
        command_type = (
            "STRATEGY_REVERSE" if decision.action == "REVERSE" else "ENTRY_INTENT"
        )
        payload = (
            {
                "symbol": signal.symbol,
                "decisionId": decision_id,
                "decision": decision.as_dict(),
                "entryIntent": intent.as_dict(),
            }
            if command_type == "STRATEGY_REVERSE"
            else intent.as_dict()
        )
        command_id = self.journal.enqueue_strategy_command(
            decision_id, command_type, payload
        )
        if command_id is None:
            raise RuleViolation("strategy decision could not be queued uniquely")
        self.journal.append_audit(
            "strategy",
            f"{decision.action}_QUEUED",
            symbol=signal.symbol,
            correlation_id=decision_id,
            payload={
                "target_position": decision.target_position,
                "command_id": command_id,
            },
        )
        return StrategySubmission(
            "queued",
            signal.signal_id,
            intent,
            command_id,
            action=decision.action,
            decision_id=decision_id,
        )

    def _submit_exit_decision(
        self,
        decision: StrategyExitDecision,
        *,
        execute: bool,
        now_ms: int,
        max_signal_age_seconds: int,
    ) -> StrategySubmission:
        if self.research_only:
            raise RuleViolation("research-only strategy decisions cannot be submitted")
        if not self.settings.is_testnet:
            raise ConfigurationError("strategy execution adapter is restricted to Testnet")
        if (
            decision.instance_id != self.instance.instance_id
            or decision.strategy != self.instance.implementation
            or decision.version != self.implementation_version
            or decision.symbol != self.instance.symbol
            or decision.interval != self.instance.interval
        ):
            raise RuleViolation("strategy exit decision does not match configuration")
        if max_signal_age_seconds < 1 or max_signal_age_seconds > 300:
            raise ValueError("max signal age must be between 1 and 300 seconds")
        age = now_ms - decision.candle_close_time
        if age < -1000:
            raise RuleViolation("strategy exit candle close time is in the future")
        if age > max_signal_age_seconds * 1000:
            raise RuleViolation("strategy exit decision is stale")
        active_instance = self.journal.get_control("active_strategy_instance", "")
        if active_instance != decision.instance_id:
            raise RuleViolation(
                f"strategy instance is not active for execution: {decision.instance_id}"
            )
        if self.journal.get_control("user_stream_healthy", "false") != "true":
            raise EntryPaused("user stream is not healthy")
        market_key = f"market_data_{decision.symbol}_{decision.interval}_healthy"
        if self.journal.get_control(market_key, "false") != "true":
            raise EntryPaused("strategy market data stream is not healthy")
        if execute and not lock_owner_active(self.settings.lock_path):
            raise RuleViolation("strategy execution requires a running daemon")
        active_intent = self.journal.latest_active_intent(decision.symbol)
        active_orders = self.journal.active_orders(decision.symbol)
        if active_intent is None or not active_orders:
            raise RuleViolation("strategy exit requires an active protected local position")
        expected_side = "BUY" if decision.current_position == "LONG" else "SELL"
        if str(active_intent.get("side")) != expected_side:
            raise RuleViolation("strategy exit does not match the active local side")
        decision_id = decision.decision_id
        if self.journal.strategy_signal_command_exists(decision_id):
            raise RuleViolation("strategy exit decision has already been submitted")
        if self.journal.pending_entry_command_exists():
            raise RuleViolation("another strategy command is already pending")
        if not execute:
            return StrategySubmission(
                "preview",
                decision_id,
                None,
                None,
                action="EXIT",
                decision_id=decision_id,
            )
        payload = {
            "symbol": decision.symbol,
            "decisionId": decision_id,
            "decision": decision.as_dict(),
        }
        command_id = self.journal.enqueue_strategy_command(
            decision_id, "STRATEGY_EXIT", payload
        )
        if command_id is None:
            raise RuleViolation("strategy exit decision could not be queued uniquely")
        self.journal.append_audit(
            "strategy",
            "EXIT_QUEUED",
            symbol=decision.symbol,
            correlation_id=decision_id,
            payload={"command_id": command_id},
        )
        return StrategySubmission(
            "queued",
            decision_id,
            None,
            command_id,
            action="EXIT",
            decision_id=decision_id,
        )

    def _validate_signal(
        self, signal: StrategySignal, now_ms: int, max_signal_age_seconds: int
    ) -> None:
        if self.research_only:
            raise RuleViolation("research-only strategy signals cannot be submitted")
        if self.testnet_only and not self.settings.is_testnet:
            raise ConfigurationError("strategy implementation is restricted to Testnet")
        if not self.settings.is_testnet:
            raise ConfigurationError("strategy execution adapter is restricted to Testnet")
        if dict(signal.indicators).get("position_size_mode") == "full_equity":
            raise RuleViolation(
                "full-equity strategy signals are research-only and cannot be submitted"
            )
        if signal.stop_price is None:
            raise RuleViolation("strategy execution requires an exchange-side stop")
        if not signal.instance_id:
            raise RuleViolation("strategy signal has no configured instance ID")
        if signal.instance_id != self.instance.instance_id:
            raise RuleViolation("strategy signal does not match configured instance")
        if signal.strategy != self.instance.implementation:
            raise RuleViolation("strategy signal implementation does not match configuration")
        if signal.version != self.implementation_version:
            raise RuleViolation("strategy signal version does not match registry")
        if signal.symbol != self.instance.symbol or signal.interval != self.instance.interval:
            raise RuleViolation("strategy signal market does not match configuration")
        active_instance = self.journal.get_control("active_strategy_instance", "")
        if active_instance != signal.instance_id:
            raise RuleViolation(
                f"strategy instance is not active for execution: {signal.instance_id}"
            )
        if signal.risk_usdt > self.max_risk_usdt:
            raise RuleViolation("strategy risk exceeds the 1 USDT Testnet cap")
        if signal.leverage > self.max_leverage:
            raise RuleViolation("strategy leverage exceeds the 3x Testnet cap")
        if max_signal_age_seconds < 1 or max_signal_age_seconds > 300:
            raise ValueError("max signal age must be between 1 and 300 seconds")
        age = now_ms - signal.candle_close_time
        if age < -1000:
            raise RuleViolation("strategy signal candle close time is in the future")
        if age > max_signal_age_seconds * 1000:
            raise RuleViolation("strategy signal is stale")

    @staticmethod
    def _minimum_stop_bps(signal: StrategySignal) -> Decimal | None:
        value = dict(signal.indicators).get("min_stop_bps")
        if value is None:
            return None
        result = Decimal(value)
        return result if result > 0 else None

    @staticmethod
    def _maximum_stop_bps(signal: StrategySignal) -> Decimal | None:
        value = dict(signal.indicators).get("max_stop_bps")
        if value is None:
            return None
        result = Decimal(value)
        return result if result > 0 else None

    def _validate_local_state(
        self, signal: StrategySignal, signal_id: str, *, execute: bool
    ) -> None:
        self._validate_local_health(signal, execute=execute)
        if self.journal.latest_active_intent(signal.symbol):
            raise RuleViolation(f"{signal.symbol} already has an active local intent")
        if self.journal.active_orders(signal.symbol):
            raise RuleViolation(f"{signal.symbol} already has active local orders")
        if self.journal.strategy_signal_command_exists(signal_id):
            raise RuleViolation("strategy signal has already been submitted")
        if self.journal.pending_entry_command_exists():
            raise RuleViolation("another entry intent is already pending")

    def _validate_local_health(
        self, signal: StrategySignal, *, execute: bool
    ) -> None:
        if self.journal.get_control("entry_enabled", "false") != "true":
            raise EntryPaused("new entries are paused")
        if self.journal.get_control("user_stream_healthy", "false") != "true":
            raise EntryPaused("user stream is not healthy")
        market_key = f"market_data_{signal.symbol}_{signal.interval}_healthy"
        if self.journal.get_control(market_key, "false") != "true":
            raise EntryPaused("strategy market data stream is not healthy")
        if execute and not lock_owner_active(self.settings.lock_path):
            raise RuleViolation("strategy execution requires a running daemon")

    @staticmethod
    def signal_id(signal: StrategySignal) -> str:
        return signal.signal_id
