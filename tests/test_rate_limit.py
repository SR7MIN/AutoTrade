import unittest

from autotrade.errors import LocalRateLimitExceeded
from autotrade.rate_limit import RateLimitGuard


class RateLimitGuardTests(unittest.TestCase):
    def test_blocks_normal_requests_but_reserves_risk_actions(self) -> None:
        guard = RateLimitGuard(utilization_limit=0.8)
        guard.configure(
            [
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "MINUTE",
                    "intervalNum": 1,
                    "limit": 100,
                }
            ]
        )
        guard.record({"x-mbx-used-weight-1m": "80"})
        with self.assertRaises(LocalRateLimitExceeded):
            guard.check(risk_reducing=False)
        guard.check(risk_reducing=True)


if __name__ == "__main__":
    unittest.main()
