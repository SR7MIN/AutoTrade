from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from importlib import metadata
from typing import Any, Callable

from .base import Strategy
from .ema_atr import EmaAtrStrategy
from .lifecycle_pulse import LifecyclePulseStrategy
from .multi_divergence import MultiDivergenceReversalStrategy


StrategyFactory = Callable[[str, str, str, dict[str, Any]], Strategy]


@dataclass(frozen=True, slots=True)
class StrategyRegistration:
    name: str
    version: str
    description: str
    factory: StrategyFactory
    testnet_only: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "testnetOnly": self.testnet_only,
        }


class StrategyRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, StrategyRegistration] = {}

    def register(self, registration: StrategyRegistration) -> None:
        if not registration.name.strip():
            raise ValueError("strategy registration name is required")
        if registration.name in self._registrations:
            raise ValueError(f"strategy is already registered: {registration.name}")
        self._registrations[registration.name] = registration

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))

    def registrations(self) -> tuple[StrategyRegistration, ...]:
        return tuple(self._registrations[name] for name in self.names())

    def registration(self, name: str) -> StrategyRegistration:
        try:
            return self._registrations[name]
        except KeyError as exc:
            raise ValueError(f"unknown strategy implementation: {name}") from exc

    def create(
        self,
        name: str,
        *,
        instance_id: str,
        symbol: str,
        interval: str,
        parameters: dict[str, Any] | None = None,
    ) -> Strategy:
        registration = self.registration(name)
        return registration.factory(instance_id, symbol, interval, parameters or {})


def _ema_atr_factory(
    instance_id: str, symbol: str, interval: str, parameters: dict[str, Any]
) -> Strategy:
    allowed = {
        "fast_period",
        "slow_period",
        "atr_period",
        "stop_atr_multiple",
        "reward_risk",
        "risk_usdt",
        "leverage",
        "margin_utilization",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(f"unknown ema-atr-v1 parameters: {sorted(unknown)}")
    integer_values = {"fast_period", "slow_period", "atr_period", "leverage"}
    values: dict[str, Any] = {}
    for key, value in parameters.items():
        values[key] = int(value) if key in integer_values else Decimal(str(value))
    return EmaAtrStrategy(
        instance_id=instance_id,
        symbol=symbol,
        interval=interval,
        **values,
    )


def _lifecycle_pulse_factory(
    instance_id: str, symbol: str, interval: str, parameters: dict[str, Any]
) -> Strategy:
    allowed = {
        "stop_bps",
        "take_profit_bps",
        "risk_usdt",
        "leverage",
        "margin_utilization",
        "cooldown_bars",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(
            f"unknown lifecycle-pulse-testnet-v1 parameters: {sorted(unknown)}"
        )
    integer_values = {"leverage", "cooldown_bars"}
    values: dict[str, Any] = {}
    for key, value in parameters.items():
        values[key] = int(value) if key in integer_values else Decimal(str(value))
    return LifecyclePulseStrategy(
        instance_id=instance_id,
        symbol=symbol,
        interval=interval,
        **values,
    )


def _multi_divergence_factory(
    instance_id: str, symbol: str, interval: str, parameters: dict[str, Any]
) -> Strategy:
    allowed = {
        "pivot_period",
        "pivot_source",
        "max_pivots",
        "max_bars_to_check",
        "divergence_types",
        "indicators",
        "min_entry_divergences",
        "min_reverse_divergences",
        "count_mode",
        "conflict_policy",
        "require_confirmation",
        "atr_period",
        "stop_atr_buffer",
        "min_stop_atr",
        "max_stop_atr",
        "safety_take_profit_rr",
        "risk_usdt",
        "leverage",
        "margin_utilization",
        "allow_pyramiding",
    }
    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(
            f"unknown multi-divergence-reversal-v1 parameters: {sorted(unknown)}"
        )
    fixed_values = {
        "pivot_source": "high_low",
        "count_mode": "unique_indicators",
        "conflict_policy": "hold",
        "require_confirmation": True,
        "allow_pyramiding": False,
    }
    for key, expected in fixed_values.items():
        if key in parameters and parameters[key] != expected:
            raise ValueError(f"{key} must be {expected!r} in the first divergence version")
    integer_values = {
        "pivot_period",
        "max_pivots",
        "max_bars_to_check",
        "min_entry_divergences",
        "min_reverse_divergences",
        "atr_period",
        "leverage",
    }
    decimal_values = {
        "stop_atr_buffer",
        "min_stop_atr",
        "max_stop_atr",
        "safety_take_profit_rr",
        "risk_usdt",
        "margin_utilization",
    }
    ignored = set(fixed_values)
    values: dict[str, Any] = {}
    for key, value in parameters.items():
        if key in ignored:
            continue
        if key in integer_values:
            values[key] = int(value)
        elif key in decimal_values:
            values[key] = Decimal(str(value))
        elif key in {"divergence_types", "indicators"}:
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a TOML array")
            values[key] = tuple(str(item) for item in value)
    return MultiDivergenceReversalStrategy(
        instance_id=instance_id,
        symbol=symbol,
        interval=interval,
        **values,
    )


BUILTIN_STRATEGIES = StrategyRegistry()
BUILTIN_STRATEGIES.register(
    StrategyRegistration(
        name=EmaAtrStrategy.name,
        version=EmaAtrStrategy.version,
        description="EMA crossover with Wilder ATR bracket levels",
        factory=_ema_atr_factory,
    )
)
BUILTIN_STRATEGIES.register(
    StrategyRegistration(
        name=MultiDivergenceReversalStrategy.name,
        version=MultiDivergenceReversalStrategy.version,
        description=(
            "Confirmed 5m regular/hidden multi-indicator divergence with target-position reversal"
        ),
        factory=_multi_divergence_factory,
    )
)
BUILTIN_STRATEGIES.register(
    StrategyRegistration(
        name=LifecyclePulseStrategy.name,
        version=LifecyclePulseStrategy.version,
        description="Every-bar directional pulse for manual Testnet lifecycle validation",
        factory=_lifecycle_pulse_factory,
        testnet_only=True,
    )
)


def load_installed_strategies(
    registry: StrategyRegistry = BUILTIN_STRATEGIES,
) -> StrategyRegistry:
    discovered = metadata.entry_points()
    entry_points = (
        discovered.select(group="autotrade.strategies")
        if hasattr(discovered, "select")
        else discovered.get("autotrade.strategies", ())
    )
    for entry_point in entry_points:
        value = entry_point.load()
        registration = value() if callable(value) and not isinstance(
            value, StrategyRegistration
        ) else value
        if not isinstance(registration, StrategyRegistration):
            raise ValueError(
                f"strategy entry point must provide StrategyRegistration: {entry_point.name}"
            )
        if registration.name in registry.names():
            existing = registry.registration(registration.name)
            if existing.version != registration.version:
                raise ValueError(
                    f"strategy plugin conflicts with registered version: {registration.name}"
                )
            continue
        registry.register(registration)
    return registry
