from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .candles import Candle
from .strategy import (
    Strategy,
    StrategyDecision,
    StrategyExitDecision,
    StrategySignal,
)


@dataclass(frozen=True, slots=True)
class BacktestRejection:
    candle_open_time: int
    reason: str
    signal: StrategySignal | None
    decision_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "candleOpenTime": self.candle_open_time,
            "reason": self.reason,
            "signal": self.signal.as_dict() if self.signal is not None else None,
            "decisionId": self.decision_id,
        }


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    side: str
    signal_time: int
    entry_time: int
    exit_time: int
    entry_price: Decimal
    exit_price: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    quantity: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    exit_reason: str
    holding_bars: int
    max_favorable_excursion: Decimal
    max_adverse_excursion: Decimal
    mfe_r: Decimal
    mae_r: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "signalTime": self.signal_time,
            "entryTime": self.entry_time,
            "exitTime": self.exit_time,
            "entryPrice": str(self.entry_price),
            "exitPrice": str(self.exit_price),
            "stopPrice": str(self.stop_price) if self.stop_price is not None else None,
            "takeProfitPrice": (
                str(self.take_profit_price)
                if self.take_profit_price is not None
                else None
            ),
            "quantity": str(self.quantity),
            "grossPnl": str(self.gross_pnl),
            "fees": str(self.fees),
            "netPnl": str(self.net_pnl),
            "exitReason": self.exit_reason,
            "holdingBars": self.holding_bars,
            "maxFavorableExcursion": str(self.max_favorable_excursion),
            "maxAdverseExcursion": str(self.max_adverse_excursion),
            "mfeR": str(self.mfe_r),
            "maeR": str(self.mae_r),
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    version: str
    strategy_instance: str
    symbol: str
    interval: str
    candle_count: int
    signal_count: int
    initial_balance: Decimal
    final_balance: Decimal
    max_drawdown_fraction: Decimal
    trades: tuple[BacktestTrade, ...]
    rejections: tuple[BacktestRejection, ...]

    def as_dict(self) -> dict[str, object]:
        winners = sum(1 for trade in self.trades if trade.net_pnl > 0)
        net_pnl = self.final_balance - self.initial_balance
        trade_count = len(self.trades)
        return {
            "strategy": self.strategy,
            "version": self.version,
            "strategyInstance": self.strategy_instance,
            "symbol": self.symbol,
            "interval": self.interval,
            "candleCount": self.candle_count,
            "signalCount": self.signal_count,
            "tradeCount": trade_count,
            "rejectionCount": len(self.rejections),
            "initialBalance": str(self.initial_balance),
            "finalBalance": str(self.final_balance),
            "netPnl": str(net_pnl),
            "winRate": str(
                Decimal(winners) / Decimal(len(self.trades)) if self.trades else Decimal(0)
            ),
            "maxDrawdownFraction": str(self.max_drawdown_fraction),
            "averageHoldingBars": str(
                Decimal(sum(trade.holding_bars for trade in self.trades))
                / Decimal(trade_count)
                if trade_count
                else Decimal(0)
            ),
            "averageMfeR": str(
                sum((trade.mfe_r for trade in self.trades), Decimal(0))
                / Decimal(trade_count)
                if trade_count
                else Decimal(0)
            ),
            "averageMaeR": str(
                sum((trade.mae_r for trade in self.trades), Decimal(0))
                / Decimal(trade_count)
                if trade_count
                else Decimal(0)
            ),
            "trades": [trade.as_dict() for trade in self.trades],
            "rejections": [rejection.as_dict() for rejection in self.rejections],
        }


@dataclass(slots=True)
class _OpenPosition:
    signal: StrategySignal
    entry_time: int
    entry_price: Decimal
    quantity: Decimal
    entry_index: int
    max_favorable_excursion: Decimal = Decimal(0)
    max_adverse_excursion: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class _PendingAction:
    action: str
    signal: StrategySignal | None
    current_position: str
    exit_decision: StrategyExitDecision | None = None


class BacktestEngine:
    def __init__(
        self,
        *,
        initial_balance: Decimal = Decimal("1000"),
        fee_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("10"),
        cooldown_bars: int = 3,
    ) -> None:
        if initial_balance <= 0:
            raise ValueError("initial_balance must be positive")
        if fee_bps < 0 or slippage_bps < 0:
            raise ValueError("cost assumptions cannot be negative")
        if cooldown_bars < 0:
            raise ValueError("cooldown_bars cannot be negative")
        self.initial_balance = Decimal(initial_balance)
        self.fee_rate = Decimal(fee_bps) / Decimal("10000")
        self.slippage_rate = Decimal(slippage_bps) / Decimal("10000")
        self.cooldown_bars = cooldown_bars

    def run(self, candles: Sequence[Candle], strategy: Strategy) -> BacktestResult:
        values = list(candles)
        self._validate_candles(values)
        strategy.reset()
        balance = self.initial_balance
        peak_balance = balance
        max_drawdown = Decimal(0)
        signal_count = 0
        pending: _PendingAction | None = None
        position: _OpenPosition | None = None
        trades: list[BacktestTrade] = []
        rejections: list[BacktestRejection] = []
        cooldown_signal_until_index = -1

        def close_position(
            candle_index: int,
            candle: Candle,
            raw_exit: Decimal,
            reason: str,
            *,
            exit_time: int | None = None,
            apply_cooldown: bool = True,
        ) -> None:
            nonlocal balance, peak_balance, max_drawdown, position
            nonlocal cooldown_signal_until_index
            assert position is not None
            exit_price = self._exit_price(raw_exit, position.signal.side)
            if position.signal.side == "BUY":
                gross = (exit_price - position.entry_price) * position.quantity
            else:
                gross = (position.entry_price - exit_price) * position.quantity
            fees = (
                position.entry_price * position.quantity + exit_price * position.quantity
            ) * self.fee_rate
            net = gross - fees
            initial_risk = (
                abs(position.entry_price - position.signal.stop_price)
                if position.signal.stop_price is not None
                else Decimal(0)
            )
            at_open = exit_time == candle.open_time
            holding_bars = max(
                1,
                candle_index - position.entry_index + (0 if at_open else 1),
            )
            balance += net
            trades.append(
                BacktestTrade(
                    side=position.signal.side,
                    signal_time=position.signal.candle_close_time,
                    entry_time=position.entry_time,
                    exit_time=candle.close_time if exit_time is None else exit_time,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    stop_price=position.signal.stop_price,
                    take_profit_price=position.signal.take_profit_price,
                    quantity=position.quantity,
                    gross_pnl=gross,
                    fees=fees,
                    net_pnl=net,
                    exit_reason=reason,
                    holding_bars=holding_bars,
                    max_favorable_excursion=position.max_favorable_excursion,
                    max_adverse_excursion=position.max_adverse_excursion,
                    mfe_r=(
                        position.max_favorable_excursion / initial_risk
                        if initial_risk > 0
                        else Decimal(0)
                    ),
                    mae_r=(
                        position.max_adverse_excursion / initial_risk
                        if initial_risk > 0
                        else Decimal(0)
                    ),
                )
            )
            peak_balance = max(peak_balance, balance)
            if peak_balance > 0:
                max_drawdown = max(max_drawdown, (peak_balance - balance) / peak_balance)
            position = None
            if apply_cooldown:
                cooldown_signal_until_index = candle_index + self.cooldown_bars - 1

        def open_position(
            candle_index: int, candle: Candle, signal: StrategySignal
        ) -> None:
            nonlocal position
            entry_price = self._entry_price(Decimal(candle.open), signal.side)
            stop_valid = (
                signal.stop_price is None
                or signal.stop_price < entry_price
                if signal.side == "BUY"
                else signal.stop_price is None or entry_price < signal.stop_price
            )
            target_valid = (
                signal.take_profit_price is None
                or entry_price < signal.take_profit_price
                if signal.side == "BUY"
                else signal.take_profit_price is None
                or signal.take_profit_price < entry_price
            )
            valid_gap = stop_valid and target_valid
            if not valid_gap:
                rejections.append(
                    BacktestRejection(candle.open_time, "INVALID_ENTRY_GAP", signal)
                )
                return
            sizing_mode = dict(signal.indicators).get(
                "position_size_mode", "risk_budget"
            )
            if sizing_mode == "full_equity":
                quantity = balance / entry_price if balance > 0 else Decimal(0)
            else:
                if signal.stop_price is None:
                    rejections.append(
                        BacktestRejection(candle.open_time, "STOP_REQUIRED", signal)
                    )
                    return
                minimum_stop_bps = self._minimum_stop_bps(signal)
                actual_stop_bps = (
                    abs(entry_price - signal.stop_price)
                    / entry_price
                    * Decimal("10000")
                )
                if actual_stop_bps < minimum_stop_bps:
                    rejections.append(
                        BacktestRejection(
                            candle.open_time, "STOP_DISTANCE_BELOW_MINIMUM", signal
                        )
                    )
                    return
                maximum_stop_bps = self._maximum_stop_bps(signal)
                if (
                    maximum_stop_bps is not None
                    and actual_stop_bps > maximum_stop_bps
                ):
                    rejections.append(
                        BacktestRejection(
                            candle.open_time, "STOP_DISTANCE_ABOVE_MAXIMUM", signal
                        )
                    )
                    return
                expected_stop_exit = self._exit_price(signal.stop_price, signal.side)
                stop_loss_per_unit = abs(entry_price - expected_stop_exit)
                stop_fees_per_unit = (entry_price + expected_stop_exit) * self.fee_rate
                risk_quantity = signal.risk_usdt / (
                    stop_loss_per_unit + stop_fees_per_unit
                )
                margin_quantity = (
                    balance
                    * signal.margin_utilization
                    * Decimal(signal.leverage)
                    / entry_price
                )
                quantity = min(risk_quantity, margin_quantity)
            if quantity <= 0:
                rejections.append(
                    BacktestRejection(candle.open_time, "INSUFFICIENT_BALANCE", signal)
                )
                return
            position = _OpenPosition(
                signal=signal,
                entry_time=candle.open_time,
                entry_price=entry_price,
                quantity=quantity,
                entry_index=candle_index,
            )

        for candle_index, candle in enumerate(values):
            if pending is not None:
                if pending.action == "EXIT":
                    actual = (
                        "FLAT"
                        if position is None
                        else "LONG" if position.signal.side == "BUY" else "SHORT"
                    )
                    if position is None or actual != pending.current_position:
                        rejections.append(
                            BacktestRejection(
                                candle.open_time,
                                "EXIT_POSITION_MISMATCH",
                                None,
                                (
                                    pending.exit_decision.decision_id
                                    if pending.exit_decision is not None
                                    else None
                                ),
                            )
                        )
                    else:
                        close_position(
                            candle_index,
                            candle,
                            Decimal(candle.open),
                            self._strategy_exit_reason(pending.exit_decision),
                            exit_time=candle.open_time,
                        )
                elif pending.action == "REVERSE":
                    assert pending.signal is not None
                    expected_side = "BUY" if pending.current_position == "LONG" else "SELL"
                    if position is None or position.signal.side != expected_side:
                        rejections.append(
                            BacktestRejection(
                                candle.open_time,
                                "REVERSAL_POSITION_MISMATCH",
                                pending.signal,
                            )
                        )
                    else:
                        close_position(
                            candle_index,
                            candle,
                            Decimal(candle.open),
                            "REVERSE",
                            exit_time=candle.open_time,
                            apply_cooldown=False,
                        )
                        open_position(candle_index, candle, pending.signal)
                elif position is not None:
                    assert pending.signal is not None
                    rejections.append(
                        BacktestRejection(
                            candle.open_time, "POSITION_OPEN", pending.signal
                        )
                    )
                else:
                    assert pending.signal is not None
                    open_position(candle_index, candle, pending.signal)
                pending = None

            if position is not None:
                high = Decimal(candle.high)
                low = Decimal(candle.low)
                signal = position.signal
                if signal.side == "BUY":
                    stop_hit = (
                        signal.stop_price is not None and low <= signal.stop_price
                    )
                    take_profit_hit = (
                        signal.take_profit_price is not None
                        and high >= signal.take_profit_price
                    )
                else:
                    stop_hit = (
                        signal.stop_price is not None and high >= signal.stop_price
                    )
                    take_profit_hit = (
                        signal.take_profit_price is not None
                        and low <= signal.take_profit_price
                    )
                if stop_hit:
                    assert signal.stop_price is not None
                    position.max_adverse_excursion = max(
                        position.max_adverse_excursion,
                        abs(position.entry_price - signal.stop_price),
                    )
                    close_position(candle_index, candle, signal.stop_price, "STOP")
                else:
                    if signal.side == "BUY":
                        favorable = max(Decimal(0), high - position.entry_price)
                        adverse = max(Decimal(0), position.entry_price - low)
                    else:
                        favorable = max(Decimal(0), position.entry_price - low)
                        adverse = max(Decimal(0), high - position.entry_price)
                    position.max_favorable_excursion = max(
                        position.max_favorable_excursion, favorable
                    )
                    position.max_adverse_excursion = max(
                        position.max_adverse_excursion, adverse
                    )
                if position is not None and take_profit_hit and not stop_hit:
                    assert signal.take_profit_price is not None
                    close_position(
                        candle_index, candle, signal.take_profit_price, "TAKE_PROFIT"
                    )

            if hasattr(strategy, "set_position"):
                current = (
                    "FLAT"
                    if position is None
                    else "LONG" if position.signal.side == "BUY" else "SHORT"
                )
                strategy.set_position(current)  # type: ignore[attr-defined]
            output = strategy.on_candle(candle)
            if output is None:
                continue
            signal_count += 1
            if isinstance(output, StrategyExitDecision):
                actual_position = (
                    "FLAT"
                    if position is None
                    else "LONG" if position.signal.side == "BUY" else "SHORT"
                )
                if (
                    output.symbol != candle.symbol
                    or output.interval != candle.interval
                    or output.candle_open_time != candle.open_time
                    or output.candle_close_time != candle.close_time
                ):
                    rejections.append(
                        BacktestRejection(
                            candle.open_time,
                            "INVALID_EXIT_CONTEXT",
                            None,
                            output.decision_id,
                        )
                    )
                elif pending is not None:
                    rejections.append(
                        BacktestRejection(
                            candle.open_time,
                            "PENDING_ENTRY",
                            None,
                            output.decision_id,
                        )
                    )
                elif actual_position != output.current_position:
                    rejections.append(
                        BacktestRejection(
                            candle.open_time,
                            "EXIT_POSITION_MISMATCH",
                            None,
                            output.decision_id,
                        )
                    )
                else:
                    pending = _PendingAction(
                        "EXIT", None, output.current_position, output
                    )
                continue
            decision = output if isinstance(output, StrategyDecision) else None
            signal = decision.entry_signal if decision is not None else output
            if (
                signal.symbol != candle.symbol
                or signal.interval != candle.interval
                or signal.candle_open_time != candle.open_time
                or signal.candle_close_time != candle.close_time
            ):
                rejections.append(
                    BacktestRejection(candle.open_time, "INVALID_SIGNAL_CONTEXT", signal)
                )
            elif pending is not None:
                rejections.append(
                    BacktestRejection(candle.open_time, "PENDING_ENTRY", signal)
                )
            elif decision is not None and decision.action == "REVERSE":
                actual_position = (
                    "FLAT"
                    if position is None
                    else "LONG" if position.signal.side == "BUY" else "SHORT"
                )
                if actual_position != decision.current_position:
                    rejections.append(
                        BacktestRejection(
                            candle.open_time, "REVERSAL_POSITION_MISMATCH", signal
                        )
                    )
                else:
                    pending = _PendingAction("REVERSE", signal, decision.current_position)
            elif position is not None:
                rejections.append(
                    BacktestRejection(candle.open_time, "POSITION_OPEN", signal)
                )
            elif candle_index <= cooldown_signal_until_index:
                rejections.append(BacktestRejection(candle.open_time, "COOLDOWN", signal))
            else:
                pending = _PendingAction("ENTER", signal, "FLAT")

        if pending is not None:
            rejections.append(
                BacktestRejection(
                    values[-1].open_time,
                    "NO_NEXT_CANDLE",
                    pending.signal,
                    (
                        pending.exit_decision.decision_id
                        if pending.exit_decision is not None
                        else None
                    ),
                )
            )
        if position is not None:
            close_position(
                len(values) - 1,
                values[-1],
                Decimal(values[-1].close),
                "END_OF_DATA",
            )

        return BacktestResult(
            strategy=strategy.name,
            version=strategy.version,
            strategy_instance=getattr(strategy, "instance_id", strategy.name),
            symbol=values[0].symbol,
            interval=values[0].interval,
            candle_count=len(values),
            signal_count=signal_count,
            initial_balance=self.initial_balance,
            final_balance=balance,
            max_drawdown_fraction=max_drawdown,
            trades=tuple(trades),
            rejections=tuple(rejections),
        )

    def _entry_price(self, price: Decimal, side: str) -> Decimal:
        if side == "BUY":
            return price * (Decimal(1) + self.slippage_rate)
        return price * (Decimal(1) - self.slippage_rate)

    def _exit_price(self, price: Decimal, entry_side: str) -> Decimal:
        if entry_side == "BUY":
            return price * (Decimal(1) - self.slippage_rate)
        return price * (Decimal(1) + self.slippage_rate)

    @staticmethod
    def _minimum_stop_bps(signal: StrategySignal) -> Decimal:
        value = dict(signal.indicators).get("min_stop_bps")
        return Decimal(value) if value is not None else Decimal(0)

    @staticmethod
    def _maximum_stop_bps(signal: StrategySignal) -> Decimal | None:
        value = dict(signal.indicators).get("max_stop_bps")
        return Decimal(value) if value is not None else None

    @staticmethod
    def _strategy_exit_reason(
        decision: StrategyExitDecision | None,
    ) -> str:
        reason = decision.reason.lower() if decision is not None else ""
        if "opposite" in reason and "divergence" in reason:
            return "DIVERGENCE_EXIT"
        if "trailing" in reason:
            return "ATR_TRAILING"
        if "break-even" in reason:
            return "BREAK_EVEN"
        if "time exit" in reason:
            return "TIME_EXIT"
        return "STRATEGY_EXIT"

    @staticmethod
    def _validate_candles(candles: list[Candle]) -> None:
        if not candles:
            raise ValueError("backtest requires at least one candle")
        first = candles[0]
        previous_open: int | None = None
        for candle in candles:
            if not candle.closed:
                raise ValueError("backtest accepts closed candles only")
            if candle.symbol != first.symbol or candle.interval != first.interval:
                raise ValueError("backtest candles must share symbol and interval")
            if previous_open is not None and candle.open_time <= previous_open:
                raise ValueError("backtest candles must be strictly increasing")
            previous_open = candle.open_time
