from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .errors import LocalRateLimitExceeded


@dataclass(slots=True)
class LimitSnapshot:
    request_weight_1m: int = 0
    orders_10s: int = 0
    orders_1m: int = 0
    weight_limit_1m: int = 2400
    order_limit_10s: int = 300
    order_limit_1m: int = 1200


class RateLimitGuard:
    """Conservative local guard; risk-reducing requests always retain priority."""

    def __init__(self, utilization_limit: float = 0.85) -> None:
        self.utilization_limit = utilization_limit
        self.snapshot = LimitSnapshot()
        self._minute_bucket = int(time.time() // 60)
        self._ten_second_bucket = int(time.time() // 10)
        self._lock = threading.Lock()

    def configure(self, limits: list[dict[str, Any]]) -> None:
        with self._lock:
            for limit in limits:
                kind = limit.get("rateLimitType")
                interval = limit.get("interval")
                number = int(limit.get("intervalNum", 1))
                value = int(limit.get("limit", 0))
                if kind == "REQUEST_WEIGHT" and interval == "MINUTE" and number == 1:
                    self.snapshot.weight_limit_1m = value
                elif kind == "ORDERS" and interval == "SECOND" and number == 10:
                    self.snapshot.order_limit_10s = value
                elif kind == "ORDERS" and interval == "MINUTE" and number == 1:
                    self.snapshot.order_limit_1m = value

    def record(self, headers: dict[str, str]) -> None:
        with self._lock:
            self._roll_buckets()
            if "x-mbx-used-weight-1m" in headers:
                self.snapshot.request_weight_1m = int(headers["x-mbx-used-weight-1m"])
            if "x-mbx-order-count-10s" in headers:
                self.snapshot.orders_10s = int(headers["x-mbx-order-count-10s"])
            if "x-mbx-order-count-1m" in headers:
                self.snapshot.orders_1m = int(headers["x-mbx-order-count-1m"])

    def check(self, *, risk_reducing: bool) -> None:
        if risk_reducing:
            return
        with self._lock:
            self._roll_buckets()
            checks = (
                ("request weight", self.snapshot.request_weight_1m, self.snapshot.weight_limit_1m),
                ("10-second orders", self.snapshot.orders_10s, self.snapshot.order_limit_10s),
                ("1-minute orders", self.snapshot.orders_1m, self.snapshot.order_limit_1m),
            )
            for name, used, limit in checks:
                if limit and used >= int(limit * self.utilization_limit):
                    raise LocalRateLimitExceeded(
                        f"Local {name} guard reached {used}/{limit}; risk actions remain enabled"
                    )

    def as_dict(self) -> dict[str, int | float]:
        with self._lock:
            self._roll_buckets()
            return {
                "utilization_limit": self.utilization_limit,
                "request_weight_1m": self.snapshot.request_weight_1m,
                "weight_limit_1m": self.snapshot.weight_limit_1m,
                "orders_10s": self.snapshot.orders_10s,
                "order_limit_10s": self.snapshot.order_limit_10s,
                "orders_1m": self.snapshot.orders_1m,
                "order_limit_1m": self.snapshot.order_limit_1m,
            }

    def _roll_buckets(self) -> None:
        minute = int(time.time() // 60)
        ten_second = int(time.time() // 10)
        if minute != self._minute_bucket:
            self._minute_bucket = minute
            self.snapshot.request_weight_1m = 0
            self.snapshot.orders_1m = 0
        if ten_second != self._ten_second_bucket:
            self._ten_second_bucket = ten_second
            self.snapshot.orders_10s = 0
