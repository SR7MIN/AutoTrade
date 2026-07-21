import unittest
from dataclasses import replace
from decimal import Decimal

from autotrade.backtest import BacktestEngine
from autotrade.market_data import Candle
from autotrade.strategy.base import StrategyExitDecision, StrategySignal


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

    def __init__(self, signal_indexes, *, take_profit=Decimal("120")):
        self.signal_indexes = set(signal_indexes)
        self.take_profit = take_profit

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
            take_profit_price=self.take_profit,
            risk_usdt=Decimal("1"),
            leverage=3,
            margin_utilization=Decimal("0.5"),
            indicators=(),
            reason="fixture",
        )


class FixedExitStrategy(FixedSignalStrategy):
    name = "fixed-exit"
    version = "5"
    instance_id = "fixed-exit"

    def reset(self):
        super().reset()
        self.position = "FLAT"

    def set_position(self, position):
        self.position = position

    def on_candle(self, value):
        index = value.open_time // 300_000
        if index == 0:
            signal = super().on_candle(value)
            assert signal is not None
            return signal
        if index == 2 and self.position == "LONG":
            return StrategyExitDecision(
                strategy=self.name,
                version=self.version,
                instance_id=self.instance_id,
                symbol=value.symbol,
                interval=value.interval,
                candle_open_time=value.open_time,
                candle_close_time=value.close_time,
                current_position="LONG",
                bullish_count=0,
                bearish_count=0,
                evidence=(),
                reason="fixture managed exit",
            )
        return None


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

    def test_stop_only_signal_ignores_favorable_price_until_stop(self) -> None:
        values = [
            candle(0),
            candle(1, high="500", low="95", close="300"),
            candle(2, high="500", low="89", close="100"),
        ]
        result = self.engine().run(
            values, FixedSignalStrategy({0}, take_profit=None)
        )
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "STOP")
        self.assertEqual(result.trades[0].exit_time, values[2].close_time)
        self.assertIsNone(result.trades[0].take_profit_price)

    def test_stop_costs_are_included_in_risk_sizing(self) -> None:
        values = [candle(0), candle(1, high="105", low="89")]
        result = BacktestEngine(
            fee_bps=Decimal("5"), slippage_bps=Decimal("10"), cooldown_bars=3
        ).run(values, FixedSignalStrategy({0}))
        self.assertAlmostEqual(float(result.trades[0].net_pnl), -1.0, places=12)

    def test_rejects_actual_stop_distance_above_strategy_maximum(self) -> None:
        strategy = FixedSignalStrategy({0})
        original = strategy.on_candle

        def with_maximum(value):
            signal = original(value)
            return (
                replace(signal, indicators=(("max_stop_bps", "100"),))
                if signal is not None
                else None
            )

        strategy.on_candle = with_maximum
        result = self.engine().run([candle(0), candle(1)], strategy)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.rejections[0].reason, "STOP_DISTANCE_ABOVE_MAXIMUM")

    def test_full_equity_mode_compounds_without_a_stop(self) -> None:
        strategy = FixedSignalStrategy({0})
        original = strategy.on_candle

        def full_equity(value):
            signal = original(value)
            return (
                replace(
                    signal,
                    stop_price=None,
                    take_profit_price=None,
                    indicators=(("position_size_mode", "full_equity"),),
                )
                if signal is not None
                else None
            )

        strategy.on_candle = full_equity
        result = self.engine(cooldown=0).run(
            [candle(0), candle(1), candle(2, close="110")], strategy
        )
        self.assertEqual(result.trades[0].quantity, Decimal("10"))
        self.assertEqual(result.trades[0].net_pnl, Decimal("100"))
        self.assertEqual(result.final_balance, Decimal("1100"))
        self.assertIsNone(result.trades[0].stop_price)

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

    def test_exit_decision_closes_next_open_and_records_excursions(self) -> None:
        values = [candle(index) for index in range(4)]
        result = self.engine(cooldown=0).run(values, FixedExitStrategy({0}))
        self.assertEqual(result.signal_count, 2)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "STRATEGY_EXIT")
        self.assertEqual(trade.exit_time, values[3].open_time)
        self.assertEqual(trade.holding_bars, 2)
        self.assertEqual(trade.mfe_r, Decimal("0.5"))
        self.assertEqual(trade.mae_r, Decimal("0.5"))


if __name__ == "__main__":
    unittest.main()
