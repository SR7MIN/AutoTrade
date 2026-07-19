import unittest
from decimal import Decimal

from autotrade.backtest import BacktestEngine
from autotrade.market_data import Candle
from autotrade.strategy.base import StrategySignal


def candle(index: int, *, open="100", high="105", low="95", close="100") -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="5m",
        open_time=index * 300_000,
        close_time=(index + 1) * 300_000 - 1,
        open=open,
        high=high,
        low=low,
        close=close,
        volume="10",
        trade_count=1,
        closed=True,
    )


class FixedSignalStrategy:
    name = "fixed"
    version = "1"

    def __init__(self, signal_indexes):
        self.signal_indexes = set(signal_indexes)

    def reset(self):
        self.seen = []

    def on_candle(self, value):
        self.seen.append(value.open_time)
        index = value.open_time // 300_000
        if index not in self.signal_indexes:
            return None
        return StrategySignal(
            strategy=self.name,
            version=self.version,
            symbol=value.symbol,
            interval=value.interval,
            candle_open_time=value.open_time,
            candle_close_time=value.close_time,
            side="BUY",
            reference_price=Decimal("100"),
            stop_price=Decimal("90"),
            take_profit_price=Decimal("120"),
            risk_usdt=Decimal("1"),
            leverage=3,
            margin_utilization=Decimal("0.5"),
            indicators=(),
            reason="fixture",
        )


class BacktestTests(unittest.TestCase):
    def engine(self, *, cooldown=3):
        return BacktestEngine(fee_bps=Decimal(0), slippage_bps=Decimal(0), cooldown_bars=cooldown)

    def test_signal_executes_at_next_open_and_stop_is_applied(self) -> None:
        values = [candle(0), candle(1, high="105", low="89", close="95")]
        result = self.engine().run(values, FixedSignalStrategy({0}))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_time, values[1].open_time)
        self.assertEqual(trade.exit_reason, "STOP")
        self.assertEqual(trade.net_pnl, Decimal("-1"))

    def test_stop_wins_when_stop_and_target_hit_same_candle(self) -> None:
        values = [candle(0), candle(1, high="121", low="89")]
        result = self.engine().run(values, FixedSignalStrategy({0}))
        self.assertEqual(result.trades[0].exit_reason, "STOP")

    def test_stop_costs_are_included_in_risk_sizing(self) -> None:
        values = [candle(0), candle(1, high="105", low="89")]
        result = BacktestEngine(
            fee_bps=Decimal("5"), slippage_bps=Decimal("10"), cooldown_bars=3
        ).run(values, FixedSignalStrategy({0}))
        self.assertAlmostEqual(float(result.trades[0].net_pnl), -1.0, places=12)

    def test_three_bar_cooldown_blocks_signals_until_fourth_close(self) -> None:
        values = [
            candle(0),
            candle(1, low="89"),
            candle(2),
            candle(3),
            candle(4),
            candle(5),
        ]
        strategy = FixedSignalStrategy({0, 1, 2, 3, 4})
        result = self.engine(cooldown=3).run(values, strategy)
        reasons = [item.reason for item in result.rejections]
        self.assertEqual(reasons.count("COOLDOWN"), 3)
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[1].entry_time, values[5].open_time)

    def test_replay_is_deterministic_and_strategy_sees_one_candle_at_a_time(self) -> None:
        values = [candle(index) for index in range(4)]
        strategy = FixedSignalStrategy({0})
        first = self.engine().run(values, strategy).as_dict()
        self.assertEqual(strategy.seen, [value.open_time for value in values])
        second = self.engine().run(values, strategy).as_dict()
        self.assertEqual(first, second)

    def test_rejects_non_increasing_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            self.engine().run([candle(0), candle(0)], FixedSignalStrategy(set()))


if __name__ == "__main__":
    unittest.main()
