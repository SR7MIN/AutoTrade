import os
import unittest
from unittest.mock import patch

from autotrade.config import MAINNET_REST_URL, TESTNET_REST_URL, Settings
from autotrade.errors import ConfigurationError


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_file_patcher = patch("autotrade.config.load_env_file")
        self.env_file_patcher.start()

    def tearDown(self) -> None:
        self.env_file_patcher.stop()

    def test_defaults_to_testnet(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.is_testnet)
        self.assertEqual(settings.rest_url, TESTNET_REST_URL)

    def test_mainnet_is_locked(self) -> None:
        with patch.dict(os.environ, {"BINANCE_ENV": "mainnet"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_mainnet_requires_exact_acknowledgement(self) -> None:
        environment = {
            "BINANCE_ENV": "mainnet",
            "BINANCE_ALLOW_MAINNET": "I_UNDERSTAND",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.rest_url, MAINNET_REST_URL)

    def test_private_command_requires_both_credentials(self) -> None:
        with patch.dict(os.environ, {"BINANCE_API_KEY": "key"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env(require_credentials=True)


if __name__ == "__main__":
    unittest.main()
