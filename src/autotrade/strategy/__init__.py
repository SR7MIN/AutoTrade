from __future__ import annotations

from .base import Strategy, StrategySignal
from .ema_atr import EmaAtrStrategy


def build_strategy(name: str, *, symbol: str, interval: str) -> Strategy:
    if name == EmaAtrStrategy.name:
        return EmaAtrStrategy(symbol=symbol, interval=interval)
    raise ValueError(f"unknown strategy: {name}")


__all__ = ["EmaAtrStrategy", "Strategy", "StrategySignal", "build_strategy"]
