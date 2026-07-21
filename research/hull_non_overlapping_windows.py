from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from autotrade.backtest import BacktestEngine, BacktestResult, BacktestTrade
from autotrade.candles import Candle
from autotrade.journal import OrderJournal
from autotrade.strategy_manager import StrategyManager


INSTANCE_IDS = (
    "hull-btc-long-stop",
    "hull-eth-long-stop",
    "hull-btc-all-stop",
    "hull-eth-all-stop",
    "hull-btc-long-no-stop",
    "hull-eth-long-no-stop",
    "hull-btc-all-no-stop",
    "hull-eth-all-no-stop",
)


@dataclass(frozen=True, slots=True)
class Window:
    label: str
    start: int
    end: int
    complete_year: bool


@dataclass(frozen=True, slots=True)
class EquityPoint:
    open_time: int
    close_time: int
    equity: Decimal
    position: str


def utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


WINDOWS = (
    Window("2021", utc_ms("2021-01-01"), utc_ms("2022-01-01"), True),
    Window("2022", utc_ms("2022-01-01"), utc_ms("2023-01-01"), True),
    Window("2023", utc_ms("2023-01-01"), utc_ms("2024-01-01"), True),
    Window("2024", utc_ms("2024-01-01"), utc_ms("2025-01-01"), True),
    Window("2025", utc_ms("2025-01-01"), utc_ms("2026-01-01"), True),
    Window("2026-H1", utc_ms("2026-01-01"), utc_ms("2026-07-01"), False),
)


def liquidating_equity_curve(
    candles: Sequence[Candle],
    result: BacktestResult,
    *,
    initial_balance: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
) -> tuple[EquityPoint, ...]:
    fee_rate = fee_bps / Decimal("10000")
    slippage_rate = slippage_bps / Decimal("10000")
    trades = tuple(sorted(result.trades, key=lambda item: (item.entry_time, item.exit_time)))
    points: list[EquityPoint] = []

    for candle in candles:
        realized_balance = initial_balance + sum(
            (trade.net_pnl for trade in trades if trade.exit_time <= candle.close_time),
            Decimal(0),
        )
        active = next(
            (
                trade
                for trade in trades
                if trade.entry_time <= candle.close_time < trade.exit_time
            ),
            None,
        )
        if active is None:
            equity = realized_balance
            position = "FLAT"
        else:
            raw_exit = Decimal(candle.close)
            if active.side == "BUY":
                exit_price = raw_exit * (Decimal(1) - slippage_rate)
                gross_pnl = (exit_price - active.entry_price) * active.quantity
                position = "LONG"
            else:
                exit_price = raw_exit * (Decimal(1) + slippage_rate)
                gross_pnl = (active.entry_price - exit_price) * active.quantity
                position = "SHORT"
            estimated_fees = (
                active.entry_price * active.quantity + exit_price * active.quantity
            ) * fee_rate
            equity = realized_balance + gross_pnl - estimated_fees
        points.append(
            EquityPoint(
                open_time=candle.open_time,
                close_time=candle.close_time,
                equity=equity,
                position=position,
            )
        )

    if abs(points[-1].equity - result.final_balance) > Decimal("1e-18"):
        raise ValueError(
            f"equity reconstruction mismatch for {result.strategy_instance}: "
            f"{points[-1].equity} != {result.final_balance}"
        )
    return tuple(points)


def window_result(
    window: Window,
    curve: Sequence[EquityPoint],
    trades: Sequence[BacktestTrade],
) -> dict[str, object]:
    prior = [point for point in curve if point.close_time < window.start]
    values = [
        point for point in curve if window.start <= point.open_time < window.end
    ]
    if not prior or not values:
        raise ValueError(f"missing equity boundary for window {window.label}")

    start_equity = prior[-1].equity
    end_equity = values[-1].equity
    peak = start_equity
    max_drawdown = Decimal(0)
    for point in values:
        peak = max(peak, point.equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - point.equity) / peak)

    closed_trades = [
        trade for trade in trades if window.start <= trade.exit_time < window.end
    ]
    winners = sum(1 for trade in closed_trades if trade.net_pnl > 0)
    gross_wins = sum(
        (trade.net_pnl for trade in closed_trades if trade.net_pnl > 0), Decimal(0)
    )
    gross_losses = -sum(
        (trade.net_pnl for trade in closed_trades if trade.net_pnl < 0), Decimal(0)
    )
    start_position = prior[-1].position
    end_position = values[-1].position
    return {
        "label": window.label,
        "completeYear": window.complete_year,
        "startEquity": str(start_equity),
        "endEquity": str(end_equity),
        "returnFraction": str(end_equity / start_equity - Decimal(1)),
        "markToMarketMaxDrawdownFraction": str(max_drawdown),
        "closedTradeCount": len(closed_trades),
        "winRate": str(
            Decimal(winners) / Decimal(len(closed_trades))
            if closed_trades
            else Decimal(0)
        ),
        "profitFactor": str(
            gross_wins / gross_losses if gross_losses > 0 else Decimal(0)
        ),
        "startPosition": start_position,
        "endPosition": end_position,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuous Hull replay with non-overlapping MTM windows"
    )
    parser.add_argument(
        "--database", type=Path, default=Path(".autotrade/research-mainnet-daily.db")
    )
    parser.add_argument(
        "--config", type=Path, default=Path("research/hull-variants.toml")
    )
    parser.add_argument("--initial-balance", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--fee-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("10"))
    parser.add_argument("--cooldown-bars", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manager = StrategyManager.from_toml(
        args.config, state_root=Path(".autotrade/strategies")
    )
    journal = OrderJournal(args.database)
    try:
        candle_cache: dict[tuple[str, str], tuple[Candle, ...]] = {}
        results: list[dict[str, object]] = []
        for instance_id in INSTANCE_IDS:
            strategy = manager.build(instance_id)
            key = (strategy.symbol, strategy.interval)
            if key not in candle_cache:
                candle_cache[key] = tuple(
                    Candle.from_dict(value)
                    for value in journal.candles(
                        strategy.symbol,
                        strategy.interval,
                        start_time=utc_ms("2020-01-01"),
                        end_time=utc_ms("2026-07-01"),
                    )
                )
            candles = candle_cache[key]
            replay = BacktestEngine(
                initial_balance=args.initial_balance,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
                cooldown_bars=args.cooldown_bars,
            ).run(candles, strategy)
            curve = liquidating_equity_curve(
                candles,
                replay,
                initial_balance=args.initial_balance,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
            )
            results.append(
                {
                    "instance": instance_id,
                    "symbol": strategy.symbol,
                    "direction": getattr(strategy, "direction", None),
                    "protectiveStopEnabled": getattr(
                        strategy, "protective_stop_enabled", None
                    ),
                    "fullPeriodFinalBalance": str(replay.final_balance),
                    "windows": [
                        window_result(window, curve, replay.trades)
                        for window in WINDOWS
                    ],
                }
            )
    finally:
        journal.close()

    payload = {
        "database": str(args.database),
        "config": str(args.config),
        "protocol": {
            "continuousStart": "2020-01-01",
            "continuousEndExclusive": "2026-07-01",
            "initialBalance": str(args.initial_balance),
            "feeBpsPerSide": str(args.fee_bps),
            "slippageBpsPerSide": str(args.slippage_bps),
            "cooldownBars": args.cooldown_bars,
            "equity": "daily close liquidation value including estimated exit costs",
        },
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
