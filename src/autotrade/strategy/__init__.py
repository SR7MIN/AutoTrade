from __future__ import annotations

from .base import (
    DivergenceEvidence,
    Strategy,
    StrategyDecision,
    StrategyExitDecision,
    StrategyOutput,
    StrategySignal,
)
from .ema_atr import EmaAtrStrategy
from .hull import HullSuiteFullEquityStrategy, PineHullMovingAverage, PineWeightedMovingAverage
from .lifecycle_pulse import LifecyclePulseStrategy
from .multi_divergence import MultiDivergenceReversalStrategy
from .registry import (
    BUILTIN_STRATEGIES,
    StrategyRegistration,
    StrategyRegistry,
    load_installed_strategies,
)


def build_strategy(
    name: str,
    *,
    symbol: str,
    interval: str,
    instance_id: str | None = None,
    parameters: dict[str, object] | None = None,
) -> Strategy:
    return BUILTIN_STRATEGIES.create(
        name,
        instance_id=instance_id or name,
        symbol=symbol,
        interval=interval,
        parameters=parameters,
    )


__all__ = [
    "BUILTIN_STRATEGIES",
    "EmaAtrStrategy",
    "DivergenceEvidence",
    "LifecyclePulseStrategy",
    "HullSuiteFullEquityStrategy",
    "MultiDivergenceReversalStrategy",
    "Strategy",
    "StrategyDecision",
    "StrategyExitDecision",
    "StrategyOutput",
    "StrategyRegistration",
    "StrategyRegistry",
    "StrategySignal",
    "PineHullMovingAverage",
    "PineWeightedMovingAverage",
    "build_strategy",
    "load_installed_strategies",
]
