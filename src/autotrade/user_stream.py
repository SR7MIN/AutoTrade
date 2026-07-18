from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

import websockets

from .binance_rest import BinanceRestClient
from .rules import decimal_value


RECONCILE_EVENTS = {
    "ACCOUNT_UPDATE",
    "ORDER_TRADE_UPDATE",
    "ALGO_UPDATE",
    "CONDITIONAL_ORDER_TRADE_UPDATE",
    "CONDITIONAL_ORDER_TRIGGER_REJECT",
}


class FlatPositionReconciler:
    """Remove stale sibling orders once the configured symbol becomes flat."""

    def __init__(self, client: BinanceRestClient, symbol: str) -> None:
        self.client = client
        self.symbol = symbol.upper()
        self._flat_checked = False

    def handle(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("e") not in RECONCILE_EVENTS:
            return None
        positions = self.client.positions(self.symbol)
        is_open = any(
            decimal_value(position.get("positionAmt", "0")) != 0 for position in positions
        )
        if is_open:
            self._flat_checked = False
            return None
        if self._flat_checked:
            return None
        self._flat_checked = True

        ordinary = self.client.open_orders(self.symbol)
        algo = self.client.open_algo_orders(self.symbol)
        result: dict[str, Any] = {
            "symbol": self.symbol,
            "ordinaryFound": len(ordinary),
            "algoFound": len(algo),
        }
        if ordinary:
            result["ordinaryCancel"] = self.client.cancel_all_orders(self.symbol)
        if algo:
            result["algoCancel"] = self.client.cancel_all_algo_orders(self.symbol)
        return result if ordinary or algo else None


async def stream_user_events(
    client: BinanceRestClient,
    ws_base_url: str,
    callback: Callable[[dict[str, Any]], None],
    *,
    max_session_seconds: int = 23 * 60 * 60,
    on_connected: Callable[[], None] | None = None,
) -> None:
    """Stream account events and renew the listen key every 30 minutes."""
    listen_key = (await asyncio.to_thread(client.start_user_stream))["listenKey"]
    stop_keepalive = asyncio.Event()

    async def keepalive() -> None:
        while not stop_keepalive.is_set():
            try:
                await asyncio.wait_for(stop_keepalive.wait(), timeout=30 * 60)
            except TimeoutError:
                await asyncio.to_thread(client.keepalive_user_stream)

    keepalive_task = asyncio.create_task(keepalive())
    try:
        async with websockets.connect(
            f"{ws_base_url}/ws/{listen_key}",
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as websocket:
            if on_connected:
                on_connected()
            async def consume() -> None:
                async for message in websocket:
                    event = json.loads(message)
                    result = callback(event)
                    if inspect.isawaitable(result):
                        await result
                    if event.get("e") == "listenKeyExpired":
                        raise RuntimeError("Binance listen key expired")

            consumer_task = asyncio.create_task(consume())
            rotation_task = asyncio.create_task(asyncio.sleep(max_session_seconds))
            done, pending = await asyncio.wait(
                {consumer_task, keepalive_task, rotation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task is not rotation_task:
                    task.result()
    finally:
        stop_keepalive.set()
        if not keepalive_task.done():
            keepalive_task.cancel()
        await asyncio.gather(keepalive_task, return_exceptions=True)
        try:
            await asyncio.to_thread(client.close_user_stream)
        except Exception:
            pass
