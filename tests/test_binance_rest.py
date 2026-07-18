import unittest
from pathlib import Path

import httpx

from autotrade.binance_rest import BinanceRestClient
from autotrade.config import RiskSettings
from autotrade.errors import BinanceAPIError
from tests.test_risk_control import limits


class ClientSettings:
    rest_url = "https://example.test"
    api_key = "key"
    api_secret = "secret"
    recv_window_ms = 5000
    environment = "testnet"
    ws_url = "wss://example.test"
    database_path = Path("state.db")
    log_path = Path("log.jsonl")
    lock_path = Path("writer.lock")
    risk: RiskSettings = limits()
    unprotected_action = "pause"


class BinanceRestClientTests(unittest.TestCase):
    def test_get_retries_transient_server_errors(self) -> None:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(500, json={"code": -1000, "msg": "temporary"})
            return httpx.Response(200, json={"serverTime": 123})

        client = BinanceRestClient(ClientSettings(), transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(client.server_time()["serverTime"], 123)
            self.assertEqual(calls, 3)
        finally:
            client.close()

    def test_trade_post_is_not_retried_on_server_error(self) -> None:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(500, json={"code": -1000, "msg": "temporary"})

        client = BinanceRestClient(ClientSettings(), transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(BinanceAPIError):
                client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity="0.001")
            self.assertEqual(calls, 1)
        finally:
            client.close()

    def test_timestamp_error_resyncs_once_then_resubmits(self) -> None:
        order_calls = 0
        time_calls = 0

        def handler(request):
            nonlocal order_calls, time_calls
            if request.url.path == "/fapi/v1/time":
                time_calls += 1
                return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
            order_calls += 1
            if order_calls == 1:
                return httpx.Response(400, json={"code": -1021, "msg": "timestamp outside window"})
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT", "clientOrderId": "id", "status": "FILLED",
                    "executedQty": "0.001",
                },
            )

        client = BinanceRestClient(ClientSettings(), transport=httpx.MockTransport(handler))
        try:
            result = client.new_order(
                symbol="BTCUSDT", side="BUY", type="MARKET", quantity="0.001"
            )
            self.assertEqual(result["status"], "FILLED")
            self.assertEqual(order_calls, 2)
            self.assertEqual(time_calls, 1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
