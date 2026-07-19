from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .strategy import (
    BUILTIN_STRATEGIES,
    Strategy,
    StrategyRegistry,
    load_installed_strategies,
)


INSTANCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class StrategyInstanceConfig:
    instance_id: str
    implementation: str
    enabled: bool
    symbol: str
    interval: str
    parameters: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "implementation": self.implementation,
            "enabled": self.enabled,
            "symbol": self.symbol,
            "interval": self.interval,
            "parameters": self.parameters,
        }


@dataclass(frozen=True, slots=True)
class StrategyPaths:
    root: Path
    state: Path
    log: Path
    lock: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "state": str(self.state),
            "log": str(self.log),
            "lock": str(self.lock),
        }


class StrategyManager:
    def __init__(
        self,
        instances: dict[str, StrategyInstanceConfig],
        *,
        state_root: Path,
        registry: StrategyRegistry = BUILTIN_STRATEGIES,
    ) -> None:
        self.instances = instances
        self.state_root = state_root
        self.registry = registry

    @classmethod
    def from_toml(
        cls,
        path: Path,
        *,
        state_root: Path,
        registry: StrategyRegistry = BUILTIN_STRATEGIES,
    ) -> "StrategyManager":
        load_installed_strategies(registry)
        if not path.exists():
            raise ValueError(f"strategy config does not exist: {path}")
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
        raw_instances = payload.get("instances")
        if not isinstance(raw_instances, dict) or not raw_instances:
            raise ValueError("strategy config must define at least one [instances.*] table")
        instances: dict[str, StrategyInstanceConfig] = {}
        for instance_id, raw in raw_instances.items():
            if not INSTANCE_PATTERN.fullmatch(instance_id):
                raise ValueError(f"invalid strategy instance ID: {instance_id}")
            if not isinstance(raw, dict):
                raise ValueError(f"strategy instance must be an object: {instance_id}")
            implementation = str(raw.get("implementation") or "")
            registry.registration(implementation)
            parameters = raw.get("parameters") or {}
            if not isinstance(parameters, dict):
                raise ValueError(f"strategy parameters must be an object: {instance_id}")
            instances[instance_id] = StrategyInstanceConfig(
                instance_id=instance_id,
                implementation=implementation,
                enabled=bool(raw.get("enabled", True)),
                symbol=str(raw.get("symbol") or "").upper(),
                interval=str(raw.get("interval") or ""),
                parameters=dict(parameters),
            )
            if not instances[instance_id].symbol or not instances[instance_id].interval:
                raise ValueError(f"strategy symbol and interval are required: {instance_id}")
        return cls(instances, state_root=state_root, registry=registry)

    def configured(self) -> tuple[StrategyInstanceConfig, ...]:
        return tuple(self.instances[key] for key in sorted(self.instances))

    def instance(self, instance_id: str, *, require_enabled: bool = True) -> StrategyInstanceConfig:
        try:
            value = self.instances[instance_id]
        except KeyError as exc:
            raise ValueError(f"unknown strategy instance: {instance_id}") from exc
        if require_enabled and not value.enabled:
            raise ValueError(f"strategy instance is disabled: {instance_id}")
        return value

    def build(self, instance_id: str) -> Strategy:
        value = self.instance(instance_id)
        return self.registry.create(
            value.implementation,
            instance_id=value.instance_id,
            symbol=value.symbol,
            interval=value.interval,
            parameters=value.parameters,
        )

    def paths(self, instance_id: str) -> StrategyPaths:
        self.instance(instance_id, require_enabled=False)
        root = self.state_root / instance_id
        return StrategyPaths(
            root=root,
            state=root / "state.json",
            log=root / "shadow.jsonl",
            lock=root / "shadow.lock",
        )

    def as_dict(self, *, active_instance: str | None = None) -> dict[str, Any]:
        return {
            "implementations": [value.as_dict() for value in self.registry.registrations()],
            "instances": [
                {
                    **value.as_dict(),
                    "paths": self.paths(value.instance_id).as_dict(),
                    "activeForExecution": value.instance_id == active_instance,
                }
                for value in self.configured()
            ],
            "activeExecutionInstance": active_instance,
        }
