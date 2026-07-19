from __future__ import annotations

from decimal import Decimal

from ..candles import Candle
from .base import StrategySignal
from .indicators import ExponentialMovingAverage, WilderAverageTrueRange


class EmaAtrStrategy:
    name = "ema-atr-v1"
    version = "1"

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        instance_id: str | None = None,
        fast_period: int = 20,
        slow_period: int = 50,
        atr_period: int = 14,
        stop_atr_multiple: Decimal = Decimal("2"),
        reward_risk: Decimal = Decimal("2"),
        risk_usdt: Decimal = Decimal("1"),
        leverage: int = 3,
        margin_utilization: Decimal = Decimal("0.50"),
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError("fast EMA period must be less than slow EMA period")
        if stop_atr_multiple <= 0 or reward_risk <= 0 or risk_usdt <= 0:
            raise ValueError("strategy risk and distance settings must be positive")
        if leverage < 1:
            raise ValueError("strategy leverage must be positive")
        if not Decimal("0") < margin_utilization <= Decimal("1"):
            raise ValueError("margin utilization must be in (0, 1]")
        self.symbol = symbol.upper()
        self.interval = interval
        self.instance_id = instance_id or self.name
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.stop_atr_multiple = Decimal(stop_atr_multiple)
        self.reward_risk = Decimal(reward_risk)
        self.risk_usdt = Decimal(risk_usdt)
        self.leverage = leverage
        self.margin_utilization = Decimal(margin_utilization)
        self.reset()

    def reset(self) -> None:
        self._fast = ExponentialMovingAverage(self.fast_period)
        self._slow = ExponentialMovingAverage(self.slow_period)
        self._atr = WilderAverageTrueRange(self.atr_period)
        self._previous_fast: Decimal | None = None
        self._previous_slow: Decimal | None = None
        self._last_open_time: int | None = None

    def on_candle(self, candle: Candle) -> StrategySignal | None:
        if not candle.closed:
            raise ValueError("strategy accepts closed candles only")
        if candle.symbol.upper() != self.symbol or candle.interval != self.interval:
            raise ValueError("candle does not match strategy symbol and interval")
        if self._last_open_time is not None and candle.open_time <= self._last_open_time:
            raise ValueError("strategy candles must be strictly increasing")
        self._last_open_time = candle.open_time

        close = Decimal(candle.close)
        current_fast = self._fast.update(close)
        current_slow = self._slow.update(close)
        current_atr = self._atr.update(
            Decimal(candle.high), Decimal(candle.low), close
        )
        previous_fast = self._previous_fast
        previous_slow = self._previous_slow
        self._previous_fast = current_fast
        self._previous_slow = current_slow
        if (
            previous_fast is None
            or previous_slow is None
            or current_fast is None
            or current_slow is None
            or current_atr is None
        ):
            return None

        side: str | None = None
        reason = ""
        if previous_fast <= previous_slow and current_fast > current_slow:
            side = "BUY"
            reason = "fast EMA crossed above slow EMA"
        elif previous_fast >= previous_slow and current_fast < current_slow:
            side = "SELL"
            reason = "fast EMA crossed below slow EMA"
        if side is None:
            return None

        stop_distance = current_atr * self.stop_atr_multiple
        if side == "BUY":
            stop_price = close - stop_distance
            take_profit_price = close + stop_distance * self.reward_risk
        else:
            stop_price = close + stop_distance
            take_profit_price = close - stop_distance * self.reward_risk
        if stop_price <= 0 or take_profit_price <= 0:
            return None

        return StrategySignal(
            strategy=self.name,
            version=self.version,
            symbol=self.symbol,
            interval=self.interval,
            candle_open_time=candle.open_time,
            candle_close_time=candle.close_time,
            side=side,
            reference_price=close,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            risk_usdt=self.risk_usdt,
            leverage=self.leverage,
            margin_utilization=self.margin_utilization,
            indicators=(
                ("ema_fast", str(current_fast)),
                ("ema_slow", str(current_slow)),
                ("atr", str(current_atr)),
            ),
            reason=reason,
            instance_id=self.instance_id,
        )
