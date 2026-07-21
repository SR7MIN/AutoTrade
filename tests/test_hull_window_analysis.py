from decimal import Decimal

from autotrade.backtest import BacktestResult, BacktestTrade
from autotrade.candles import Candle
from research.hull_non_overlapping_windows import (
    Window,
    liquidating_equity_curve,
    window_result,
)


def candle(index: int, close: str) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="1d",
        open_time=index * 86_400_000,
        close_time=(index + 1) * 86_400_000 - 1,
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1",
        trade_count=1,
        closed=True,
    )


def test_window_uses_mark_to_market_boundary_equity() -> None:
    candles = (candle(0, "100"), candle(1, "120"), candle(2, "110"))
    trade = BacktestTrade(
        side="BUY",
        signal_time=0,
        entry_time=candles[0].open_time,
        exit_time=candles[2].close_time,
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        stop_price=None,
        take_profit_price=None,
        quantity=Decimal("10"),
        gross_pnl=Decimal("100"),
        fees=Decimal(0),
        net_pnl=Decimal("100"),
        exit_reason="END_OF_DATA",
        holding_bars=3,
        max_favorable_excursion=Decimal("20"),
        max_adverse_excursion=Decimal(0),
        mfe_r=Decimal(0),
        mae_r=Decimal(0),
    )
    replay = BacktestResult(
        strategy="fixture",
        version="1",
        strategy_instance="fixture",
        symbol="BTCUSDT",
        interval="1d",
        candle_count=3,
        signal_count=1,
        initial_balance=Decimal("1000"),
        final_balance=Decimal("1100"),
        max_drawdown_fraction=Decimal(0),
        trades=(trade,),
        rejections=(),
    )

    curve = liquidating_equity_curve(
        candles,
        replay,
        initial_balance=Decimal("1000"),
        fee_bps=Decimal(0),
        slippage_bps=Decimal(0),
    )
    result = window_result(
        Window(
            "fixture",
            start=candles[1].open_time,
            end=candles[2].close_time + 1,
            complete_year=False,
        ),
        curve,
        replay.trades,
    )

    assert result["startEquity"] == "1000"
    assert result["endEquity"] == "1100"
    assert result["returnFraction"] == "0.1"
    assert result["markToMarketMaxDrawdownFraction"] == str(
        Decimal("100") / Decimal("1200")
    )
    assert result["closedTradeCount"] == 1
    assert result["startPosition"] == "LONG"
    assert result["endPosition"] == "FLAT"
