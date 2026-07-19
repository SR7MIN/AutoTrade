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
from .strategy import StrategySignal


@dataclass(frozen=True, slots=True)
class StrategySubmission:
    mode: str
    signal_id: str
    intent: EntryIntent
    command_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "signalId": self.signal_id,
            "commandId": self.command_id,
            "intent": self.intent.as_dict(),
        }


class TestnetStrategyAdapter:
    """Strict bridge from reviewed strategy signals to the daemon command queue."""

    allowed_strategy = "ema-atr-v1"
    allowed_symbol = "BTCUSDT"
    max_risk_usdt = 1
    max_leverage = 3

    def __init__(self, settings: Settings, journal: OrderJournal) -> None:
        self.settings = settings
        self.journal = journal

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

    def _validate_signal(
        self, signal: StrategySignal, now_ms: int, max_signal_age_seconds: int
    ) -> None:
        if not self.settings.is_testnet:
            raise ConfigurationError("strategy execution adapter is restricted to Testnet")
        if signal.strategy != self.allowed_strategy:
            raise RuleViolation(f"strategy is not approved: {signal.strategy}")
        if signal.symbol != self.allowed_symbol:
            raise RuleViolation(f"strategy symbol is not approved: {signal.symbol}")
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

    def _validate_local_state(
        self, signal: StrategySignal, signal_id: str, *, execute: bool
    ) -> None:
        if self.journal.get_control("entry_enabled", "false") != "true":
            raise EntryPaused("new entries are paused")
        if self.journal.get_control("user_stream_healthy", "false") != "true":
            raise EntryPaused("user stream is not healthy")
        market_key = f"market_data_{signal.symbol}_{signal.interval}_healthy"
        if self.journal.get_control(market_key, "false") != "true":
            raise EntryPaused("strategy market data stream is not healthy")
        if self.journal.latest_active_intent(signal.symbol):
            raise RuleViolation(f"{signal.symbol} already has an active local intent")
        if self.journal.active_orders(signal.symbol):
            raise RuleViolation(f"{signal.symbol} already has active local orders")
        if self.journal.strategy_signal_command_exists(signal_id):
            raise RuleViolation("strategy signal has already been submitted")
        if self.journal.pending_entry_command_exists():
            raise RuleViolation("another entry intent is already pending")
        if execute and not lock_owner_active(self.settings.lock_path):
            raise RuleViolation("strategy execution requires a running daemon")

    @staticmethod
    def signal_id(signal: StrategySignal) -> str:
        return signal.signal_id
