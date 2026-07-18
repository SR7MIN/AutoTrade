from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

import httpx

from .config import Settings
from .errors import BinanceAPIError, ConfigurationError
from .rate_limit import RateLimitGuard


RATE_LIMIT_HEADERS = (
    "x-mbx-used-weight-1m",
    "x-mbx-order-count-10s",
    "x-mbx-order-count-1m",
)


def _parameter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def encode_parameters(parameters: Mapping[str, Any] | Sequence[tuple[str, Any]]) -> str:
    items = parameters.items() if isinstance(parameters, Mapping) else parameters
    normalized = [(key, _parameter_value(value)) for key, value in items if value is not None]
    return urlencode(normalized)


def sign_query(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceRestClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.rest_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "autotrade-testnet/0.1"},
        )
        self._clock_offset_ms = 0
        self.last_rate_limits: dict[str, str] = {}
        self.rate_guard = RateLimitGuard()

    def __enter__(self) -> "BinanceRestClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def sync_time(self, *, risk_reducing: bool = False) -> int:
        before = int(time.time() * 1000)
        payload = self._request("GET", "/fapi/v1/time", risk_reducing=risk_reducing)
        after = int(time.time() * 1000)
        midpoint = (before + after) // 2
        self._clock_offset_ms = int(payload["serverTime"]) - midpoint
        return self._clock_offset_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    def current_server_time_ms(self) -> int:
        return self._timestamp()

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        signed: bool = False,
        api_key: bool = False,
        idempotent: bool | None = None,
        risk_reducing: bool = False,
    ) -> Any:
        if signed and (not self.settings.api_key or not self.settings.api_secret):
            raise ConfigurationError("API credentials are required for signed requests")
        if api_key and not self.settings.api_key:
            raise ConfigurationError("BINANCE_API_KEY is required")
        idempotent = method == "GET" if idempotent is None else idempotent
        max_attempts = 3 if idempotent else 1
        attempt = 0
        time_resynced = False
        while True:
            attempt += 1
            self.rate_guard.check(risk_reducing=risk_reducing)
            values: list[tuple[str, Any]] = list((params or {}).items())
            headers: dict[str, str] = {}
            if signed:
                values.extend(
                    [
                        ("timestamp", self._timestamp()),
                        ("recvWindow", self.settings.recv_window_ms),
                    ]
                )
                query = encode_parameters(values)
                values.append(("signature", sign_query(query, self.settings.api_secret)))
                api_key = True
            if api_key:
                headers["X-MBX-APIKEY"] = self.settings.api_key
            query = encode_parameters(values)
            url = path if not query else f"{path}?{query}"
            try:
                response = self._client.request(method, url, headers=headers)
            except httpx.HTTPError:
                if idempotent and attempt < max_attempts:
                    time.sleep(0.25 * (2 ** (attempt - 1)))
                    continue
                raise

            self.last_rate_limits = {
                name: response.headers[name]
                for name in RATE_LIMIT_HEADERS
                if name in response.headers
            }
            self.rate_guard.record(self.last_rate_limits)
            if not response.is_error:
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()

            try:
                error = response.json()
            except ValueError:
                error = {}
            code = error.get("code")
            message = str(error.get("msg") or response.text or response.reason_phrase)
            if signed and code == -1021 and not time_resynced:
                time_resynced = True
                self.sync_time(risk_reducing=risk_reducing)
                continue
            retryable_status = response.status_code == 429 or response.status_code >= 500
            if idempotent and retryable_status and attempt < max_attempts:
                retry_after = response.headers.get("Retry-After")
                delay = min(float(retry_after), 5.0) if retry_after else 0.25 * (2 ** (attempt - 1))
                time.sleep(delay)
                continue
            unknown = response.status_code == 503 and "unknown" in message.lower()
            raise BinanceAPIError(
                status_code=response.status_code,
                code=code,
                message=message,
                rate_limits=self.last_rate_limits.copy(),
                execution_unknown=unknown,
            )

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/ping")

    def server_time(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/time")

    def exchange_info(self, *, risk_reducing: bool = False) -> dict[str, Any]:
        payload = self._request(
            "GET", "/fapi/v1/exchangeInfo", risk_reducing=risk_reducing
        )
        self.rate_guard.configure(payload.get("rateLimits", []))
        return payload

    def mark_price(self, symbol: str, *, risk_reducing: bool = False) -> dict[str, Any]:
        return self._request(
            "GET", "/fapi/v1/premiumIndex", {"symbol": symbol},
            risk_reducing=risk_reducing
        )

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v3/account", signed=True)

    def positions(
        self, symbol: str | None = None, *, risk_reducing: bool = False
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True,
            risk_reducing=risk_reducing
        )

    def position_mode(self, *, risk_reducing: bool = False) -> dict[str, Any]:
        return self._request(
            "GET", "/fapi/v1/positionSide/dual", signed=True,
            risk_reducing=risk_reducing
        )

    def open_orders(
        self, symbol: str | None = None, *, risk_reducing: bool = False
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True,
            risk_reducing=risk_reducing
        )

    def open_algo_orders(
        self, symbol: str | None = None, *, risk_reducing: bool = False
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True,
            risk_reducing=risk_reducing
        )

    def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True
        )

    def change_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/marginType",
            {"symbol": symbol, "marginType": margin_type},
            signed=True,
        )

    def new_order(self, **params: Any) -> dict[str, Any]:
        risk_reducing = bool(params.pop("_risk_reducing", False))
        return self._request(
            "POST", "/fapi/v1/order", params, signed=True, risk_reducing=risk_reducing
        )

    def query_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
        )

    def new_algo_order(self, **params: Any) -> dict[str, Any]:
        return self._request(
            "POST", "/fapi/v1/algoOrder", params, signed=True, risk_reducing=True
        )

    def query_algo_order(
        self, *, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id, "clientAlgoId": client_algo_id},
            signed=True,
        )

    def cancel_order(
        self, symbol: str, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_order_id},
            signed=True,
            risk_reducing=True,
        )

    def cancel_algo_order(
        self, *, algo_id: int | None = None, client_algo_id: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id, "clientAlgoId": client_algo_id},
            signed=True,
            risk_reducing=True,
        )

    def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        return self._request(
            "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True,
            risk_reducing=True
        )

    def cancel_all_algo_orders(self, symbol: str) -> dict[str, Any]:
        return self._request(
            "DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True,
            risk_reducing=True
        )

    def account_trades(
        self, symbol: str, *, start_time: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_time, "limit": limit}, signed=True
        )

    def income_history(
        self, *, income_type: str | None = None, start_time: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/fapi/v1/income",
            {"incomeType": income_type, "startTime": start_time, "limit": limit}, signed=True
        )

    def leverage_brackets(self, symbol: str | None = None) -> Any:
        return self._request(
            "GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True
        )

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._request(
            "GET", "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
                "limit": limit,
            },
        )

    def start_user_stream(self) -> dict[str, Any]:
        return self._request("POST", "/fapi/v1/listenKey", api_key=True)

    def keepalive_user_stream(self) -> dict[str, Any]:
        return self._request("PUT", "/fapi/v1/listenKey", api_key=True)

    def close_user_stream(self) -> dict[str, Any]:
        return self._request("DELETE", "/fapi/v1/listenKey", api_key=True)
