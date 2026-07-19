import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autotrade.strategy import (
    BUILTIN_STRATEGIES,
    StrategyRegistration,
    StrategyRegistry,
    load_installed_strategies,
)
from autotrade.strategy_manager import StrategyManager


CONFIG = """
[instances.ema-default]
implementation = "ema-atr-v1"
enabled = true
symbol = "BTCUSDT"
interval = "5m"

[instances.ema-default.parameters]
fast_period = 20
slow_period = 50
risk_usdt = "1"

[instances.ema-fast]
implementation = "ema-atr-v1"
enabled = true
symbol = "BTCUSDT"
interval = "5m"

[instances.ema-fast.parameters]
fast_period = 10
slow_period = 30
risk_usdt = "0.5"
"""


class StrategyRegistryTests(unittest.TestCase):
    def test_builtin_registry_lists_and_builds_strategy(self) -> None:
        self.assertIn("ema-atr-v1", BUILTIN_STRATEGIES.names())
        self.assertIn("lifecycle-pulse-testnet-v1", BUILTIN_STRATEGIES.names())
        strategy = BUILTIN_STRATEGIES.create(
            "ema-atr-v1",
            instance_id="ema-test",
            symbol="BTCUSDT",
            interval="5m",
            parameters={"fast_period": 10, "slow_period": 30},
        )
        self.assertEqual(strategy.instance_id, "ema-test")
        self.assertEqual(strategy.fast_period, 10)
        lifecycle = BUILTIN_STRATEGIES.registration("lifecycle-pulse-testnet-v1")
        self.assertTrue(lifecycle.testnet_only)
        self.assertTrue(lifecycle.as_dict()["testnetOnly"])

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = StrategyRegistry()
        registration = StrategyRegistration("test", "1", "test", lambda *_: None)
        registry.register(registration)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(registration)

    def test_installed_entry_point_can_register_strategy(self) -> None:
        registration = StrategyRegistration("plugin-test", "1", "plugin", lambda *_: None)

        class EntryPoint:
            name = "plugin-test"

            def load(self):
                return registration

        class EntryPoints:
            def select(self, **kwargs):
                return [EntryPoint()] if kwargs.get("group") == "autotrade.strategies" else []

        registry = StrategyRegistry()
        with patch(
            "autotrade.strategy.registry.metadata.entry_points",
            return_value=EntryPoints(),
        ):
            load_installed_strategies(registry)
        self.assertIn("plugin-test", registry.names())


class StrategyManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = root / "strategies.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.state_root = root / "state"
        self.manager = StrategyManager.from_toml(
            self.config, state_root=self.state_root
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_multiple_instances_have_parameters_and_independent_paths(self) -> None:
        default = self.manager.build("ema-default")
        fast = self.manager.build("ema-fast")
        self.assertEqual(default.fast_period, 20)
        self.assertEqual(fast.fast_period, 10)
        self.assertEqual(fast.risk_usdt, Decimal("0.5"))
        self.assertNotEqual(
            self.manager.paths("ema-default").state,
            self.manager.paths("ema-fast").state,
        )

    def test_unknown_parameter_is_rejected_when_instance_is_built(self) -> None:
        text = CONFIG.replace('risk_usdt = "1"', 'unknown = "1"', 1)
        self.config.write_text(text, encoding="utf-8")
        manager = StrategyManager.from_toml(self.config, state_root=self.state_root)
        with self.assertRaisesRegex(ValueError, "unknown ema-atr-v1 parameters"):
            manager.build("ema-default")

    def test_disabled_instance_cannot_be_built(self) -> None:
        self.config.write_text(
            CONFIG.replace("enabled = true", "enabled = false", 1), encoding="utf-8"
        )
        manager = StrategyManager.from_toml(self.config, state_root=self.state_root)
        with self.assertRaisesRegex(ValueError, "disabled"):
            manager.build("ema-default")

    def test_lifecycle_instance_builds_with_validated_parameters(self) -> None:
        self.config.write_text(
            """
[instances.lifecycle-pulse]
implementation = "lifecycle-pulse-testnet-v1"
enabled = true
symbol = "BTCUSDT"
interval = "5m"

[instances.lifecycle-pulse.parameters]
stop_bps = "10"
take_profit_bps = "15"
risk_usdt = "1"
leverage = 3
cooldown_bars = 1
""",
            encoding="utf-8",
        )
        manager = StrategyManager.from_toml(self.config, state_root=self.state_root)
        strategy = manager.build("lifecycle-pulse")
        self.assertEqual(strategy.stop_bps, Decimal("10"))
        self.assertEqual(strategy.take_profit_bps, Decimal("15"))
        self.assertEqual(strategy.cooldown_bars, 1)
        self.assertEqual(
            manager.paths("lifecycle-pulse").log,
            self.state_root / "lifecycle-pulse" / "shadow.jsonl",
        )

    def test_lifecycle_unknown_and_unsafe_parameters_are_rejected(self) -> None:
        self.config.write_text(
            """
[instances.lifecycle-pulse]
implementation = "lifecycle-pulse-testnet-v1"
symbol = "BTCUSDT"
interval = "5m"

[instances.lifecycle-pulse.parameters]
unknown = 1
""",
            encoding="utf-8",
        )
        manager = StrategyManager.from_toml(self.config, state_root=self.state_root)
        with self.assertRaisesRegex(ValueError, "unknown lifecycle"):
            manager.build("lifecycle-pulse")

        text = self.config.read_text(encoding="utf-8").replace(
            "unknown = 1", 'stop_bps = "10000"'
        )
        self.config.write_text(text, encoding="utf-8")
        manager = StrategyManager.from_toml(self.config, state_root=self.state_root)
        with self.assertRaisesRegex(ValueError, "stop bps"):
            manager.build("lifecycle-pulse")

    def test_project_divergence_instance_builds_confirmed_5m_strategy(self) -> None:
        project_config = Path(__file__).resolve().parents[1] / "strategies.toml"
        manager = StrategyManager.from_toml(
            project_config, state_root=self.state_root
        )
        strategy = manager.build("divergence-btc-5m")
        self.assertEqual(strategy.interval, "5m")
        self.assertEqual(strategy.pivot_period, 5)
        self.assertEqual(strategy.divergence_types, ("regular", "hidden"))
        self.assertEqual(strategy.min_entry_divergences, 2)
        self.assertEqual(strategy.min_reverse_divergences, 2)


if __name__ == "__main__":
    unittest.main()
