import unittest

from autotrade.observability import redact


class ObservabilityTests(unittest.TestCase):
    def test_sensitive_fields_are_redacted_recursively(self) -> None:
        payload = {
            "api_secret": "secret",
            "nested": {"signature": "signed", "symbol": "BTCUSDT"},
        }
        result = redact(payload)
        self.assertEqual(result["api_secret"], "[REDACTED]")
        self.assertEqual(result["nested"]["signature"], "[REDACTED]")
        self.assertEqual(result["nested"]["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
