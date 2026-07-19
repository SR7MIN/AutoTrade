from __future__ import annotations

from decimal import Decimal

from ..candles import Candle
from .base import StrategySignal


class LifecyclePulseStrategy:
    """Deterministic, high-frequency Testnet lifecycle validation fixture."""

    name = "lifecycle-pulse-testnet-v1"
    version = "1"

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        instance_id: str | None = None,
        stop_bps: Decimal = Decimal("10"),
        take_profit_bps: Decimal = Decimal("15"),
        risk_usdt: Decimal = Decimal("1"),
        leverage: int = 3,
        margin_utilization: Decimal = Decimal("0.50"),
        cooldown_bars: int = 1,
    ) -> None:
        stop_bps = Decimal(stop_bps)
        take_profit_bps = Decimal(take_profit_bps)
        risk_usdt = Decimal(risk_usdt)
        margin_utilization = Decimal(margin_utilization)
        if not Decimal("0") < stop_bps < Decimal("10000"):
            raise ValueError("lifecycle stop bps must be between 0 and 10000")
        if not Decimal("0") < take_profit_bps < Decimal("10000"):
            raise ValueError("lifecycle take-profit bps must be between 0 and 10000")
        if not Decimal("0") < risk_usdt <= Decimal("1"):
            raise ValueError("lifecycle risk must be in (0, 1] USDT")
        if not 1 <= leverage <= 3:
            raise ValueError("lifecycle leverage must be between 1x and 3x")
        if not Decimal("0") < margin_utilization <= Decimal("1"):
            raise ValueError("margin utilization must be in (0, 1]")
        if cooldown_bars < 0:
            raise ValueError("lifecycle cooldown bars cannot be negative")
        self.symbol = symbol.upper()
        self.interval = interval
        self.instance_id = instance_id or self.name
        self.stop_bps = stop_bps
        self.take_profit_bps = take_profit_bps
        self.risk_usdt = risk_usdt
        self.leverage = leverage
        self.margin_utilization = margin_utilization
        self.cooldown_bars = cooldown_bars
        self.reset()

    def reset(self) -> None:
        self._last_open_time: int | None = None

    def on_candle(self, candle: Candle) -> StrategySignal:
        if not candle.closed:
            raise ValueError("strategy accepts closed candles only")
        if candle.symbol.upper() != self.symbol or candle.interval != self.interval:
            raise ValueError("candle does not match strategy symbol and interval")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError("strategy candles must be strictly increasing")
        self._last_open_time = candle.open_time

        open_price = Decimal(candle.open)
        close_price = Decimal(candle.close)
        if open_price <= 0 or close_price <= 0:
            raise ValueError("lifecycle candle prices must be positive")
        stop_rate = self.stop_bps / Decimal("10000")
        take_profit_rate = self.take_profit_bps / Decimal("10000")
        side = "BUY" if close_price >= open_price else "SELL"
        if side == "BUY":
            stop_price = close_price * (Decimal("1") - stop_rate)
            take_profit_price = close_price * (Decimal("1") + take_profit_rate)
            reason = "closed candle was flat or positive"
        else:
            stop_price = close_price * (Decimal("1") + stop_rate)
            take_profit_price = close_price * (Decimal("1") - take_profit_rate)
            reason = "closed candle was negative"
        return StrategySignal(
            strategy=self.name,
            version=self.version,
            symbol=self.symbol,
            interval=self.interval,
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            side=side,
            reference_price=close_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            risk_usdt=self.risk_usdt,
            leverage=self.leverage,
            margin_utilization=self.margin_utilization,
            indicators=(
                ("candle_open", str(open_price)),
                ("candle_close", str(close_price)),
                ("stop_bps", str(self.stop_bps)),
                ("take_profit_bps", str(self.take_profit_bps)),
            ),
            reason=reason,
            instance_id=self.instance_id,
        )
