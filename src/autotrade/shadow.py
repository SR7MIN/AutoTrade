from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .candles import Candle
from .strategy import Strategy, StrategyDecision, StrategySignal


@dataclass(frozen=True, slots=True)
class _PendingShadowAction:
    action: str
    signal: StrategySignal
    current_position: str
    decision: StrategyDecision | None = None


@dataclass(frozen=True, slots=True)
class ShadowRunResult:
    strategy: str
    version: str
    instance_id: str
    symbol: str
    interval: str
    candles_seen: int
    candles_replayed: int
    signals_emitted: int
    signals_accepted: int
    last_open_time: int | None
    virtual_position: dict[str, Any] | None
    pending_entry: dict[str, Any] | None
    cooldown_bars_remaining: int
    state_path: str
    log_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "version": self.version,
            "instanceId": self.instance_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "candlesSeen": self.candles_seen,
            "candlesReplayed": self.candles_replayed,
            "signalsEmitted": self.signals_emitted,
            "signalsAccepted": self.signals_accepted,
            "lastOpenTime": self.last_open_time,
            "virtualPosition": self.virtual_position,
            "pendingEntry": self.pending_entry,
            "cooldownBarsRemaining": self.cooldown_bars_remaining,
            "statePath": self.state_path,
            "logPath": self.log_path,
        }


class ShadowRunner:
    """Read-only strategy runner with replay-based state recovery."""

    def __init__(
        self,
        *,
        database_path: Path,
        state_path: Path,
        log_path: Path,
        cooldown_bars: int = 3,
    ) -> None:
        if cooldown_bars < 0:
            raise ValueError("cooldown_bars cannot be negative")
        self.database_path = database_path
        self.state_path = state_path
        self.log_path = log_path
        self.cooldown_bars = cooldown_bars

    def run_once(self, strategy: Strategy) -> ShadowRunResult:
        candles = self._read_candles(strategy)
        state = self._read_state()
        expected = {
            "strategy": strategy.name,
            "version": strategy.version,
            "instance_id": strategy.instance_id,
            "symbol": strategy.symbol,
            "interval": strategy.interval,
        }
        for key, value in expected.items():
            if key in state and state[key] != value:
                raise ValueError(f"shadow state {key} does not match current runner")
        cursor = state.get("last_open_time")
        if cursor is not None:
            cursor = int(cursor)
        bootstrapping = cursor is None
        baseline = state.get("started_after_open_time")
        if baseline is not None:
            baseline = int(baseline)
        elif bootstrapping:
            baseline = candles[-1].open_time if candles else None
        else:
            baseline = cursor
        replayed = 0
        emitted: list[dict[str, Any]] = []
        logged = self._logged_signal_ids()
        last_open_time: int | None = cursor
        pending: _PendingShadowAction | None = None
        position: dict[str, Any] | None = None
        cooldown_until_index = -1
        strategy.reset()
        for candle_index, candle in enumerate(candles):
            if pending is not None:
                if pending.action == "REVERSE" and position is not None:
                    actual = "LONG" if position["side"] == "BUY" else "SHORT"
                    if actual == pending.current_position:
                        position = None
                entry_price = Decimal(candle.open)
                signal = pending.signal
                valid_gap = (
                    signal.stop_price < entry_price < signal.take_profit_price
                    if signal.side == "BUY"
                    else signal.take_profit_price < entry_price < signal.stop_price
                )
                if valid_gap and position is None:
                    position = {
                        "side": signal.side,
                        "entryTime": candle.open_time,
                        "entryPrice": str(entry_price),
                        "stopPrice": str(signal.stop_price),
                        "takeProfitPrice": str(signal.take_profit_price),
                        "signalId": self._signal_id(signal),
                        "decisionId": (
                            pending.decision.decision_id
                            if pending.decision is not None
                            else None
                        ),
                    }
                pending = None

            if position is not None:
                high = Decimal(candle.high)
                low = Decimal(candle.low)
                stop = Decimal(str(position["stopPrice"]))
                target = Decimal(str(position["takeProfitPrice"]))
                if position["side"] == "BUY":
                    stop_hit = low <= stop
                    target_hit = high >= target
                else:
                    stop_hit = high >= stop
                    target_hit = low <= target
                if stop_hit or target_hit:
                    position = None
                    cooldown_until_index = candle_index + self.cooldown_bars - 1

            if hasattr(strategy, "set_position"):
                current = (
                    "FLAT"
                    if position is None
                    else "LONG" if position["side"] == "BUY" else "SHORT"
                )
                strategy.set_position(current)  # type: ignore[attr-defined]
            output = strategy.on_candle(candle)
            replayed += 1
            last_open_time = candle.open_time
            if output is not None and (
                baseline is None or candle.open_time > baseline
            ):
                strategy_decision = (
                    output if isinstance(output, StrategyDecision) else None
                )
                signal = (
                    strategy_decision.entry_signal
                    if strategy_decision is not None
                    else output
                )
                decision = "ACCEPTED"
                if strategy_decision is not None and strategy_decision.action == "REVERSE":
                    actual = (
                        "FLAT"
                        if position is None
                        else "LONG" if position["side"] == "BUY" else "SHORT"
                    )
                    if actual == strategy_decision.current_position:
                        decision = "REVERSE_ACCEPTED"
                        pending = _PendingShadowAction(
                            "REVERSE",
                            signal,
                            strategy_decision.current_position,
                            strategy_decision,
                        )
                    else:
                        decision = "REVERSAL_POSITION_MISMATCH"
                elif position is not None:
                    decision = "POSITION_OPEN"
                elif candle_index <= cooldown_until_index:
                    decision = "COOLDOWN"
                elif pending is not None:
                    decision = "PENDING_ENTRY"
                else:
                    pending = _PendingShadowAction(
                        "ENTER", signal, "FLAT", strategy_decision
                    )
                output_id = (
                    strategy_decision.decision_id
                    if strategy_decision is not None
                    else self._signal_id(signal)
                )
                if (
                    not bootstrapping
                    and candle.open_time > cursor
                    and output_id not in logged
                ):
                    item = {
                        "event": (
                            "SHADOW_DECISION"
                            if strategy_decision is not None
                            else "SHADOW_SIGNAL"
                        ),
                        "decision": decision,
                        "signalId": self._signal_id(signal),
                        "signal": signal.as_dict(),
                    }
                    if strategy_decision is not None:
                        item["decisionId"] = strategy_decision.decision_id
                        item["strategyDecision"] = strategy_decision.as_dict()
                    emitted.append(item)

        if emitted:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                for decision in emitted:
                    stream.write(json.dumps(decision, ensure_ascii=False, sort_keys=True))
                    stream.write("\n")
                stream.flush()
        if last_open_time is not None and last_open_time != cursor:
            self._write_state(
                {
                    "strategy": strategy.name,
                    "version": strategy.version,
                    "instance_id": strategy.instance_id,
                    "symbol": strategy.symbol,
                    "interval": strategy.interval,
                    "last_open_time": last_open_time,
                    "started_after_open_time": baseline,
                    "pending_entry": (
                        pending.signal.as_dict() if pending is not None else None
                    ),
                    "pending_action": (
                        {
                            "action": pending.action,
                            "currentPosition": pending.current_position,
                            "decisionId": (
                                pending.decision.decision_id
                                if pending.decision is not None
                                else None
                            ),
                        }
                        if pending is not None
                        else None
                    ),
                    "virtual_position": position,
                    "cooldown_bars_remaining": max(
                        0, cooldown_until_index - (len(candles) - 1) + 1
                    ),
                }
            )
        return ShadowRunResult(
            strategy=strategy.name,
            version=strategy.version,
            instance_id=strategy.instance_id,
            symbol=strategy.symbol,
            interval=strategy.interval,
            candles_seen=len(candles),
            candles_replayed=replayed,
            signals_emitted=len(emitted),
            signals_accepted=sum(
                1
                for decision in emitted
                if decision["decision"] in {"ACCEPTED", "REVERSE_ACCEPTED"}
            ),
            last_open_time=last_open_time,
            virtual_position=position,
            pending_entry=(pending.signal.as_dict() if pending is not None else None),
            cooldown_bars_remaining=max(
                0, cooldown_until_index - (len(candles) - 1) + 1
            ),
            state_path=str(self.state_path),
            log_path=str(self.log_path),
        )

    def _read_candles(self, strategy: Strategy) -> list[Candle]:
        uri = f"file:{self.database_path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise ValueError(f"cannot open shadow database read-only: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT symbol, interval, open_time, close_time, open, high, low, close,
                       volume, trade_count, closed
                FROM candles
                WHERE symbol = ? AND interval = ? AND closed = 1
                ORDER BY open_time
                """,
                (strategy.symbol.upper(), strategy.interval),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"cannot read shadow candles: {exc}") from exc
        finally:
            connection.close()
        candles = [Candle.from_dict(dict(row)) for row in rows]
        previous: int | None = None
        for candle in candles:
            if previous is not None and candle.open_time <= previous:
                raise ValueError("shadow candles must be strictly increasing")
            previous = candle.open_time
        return candles

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid shadow state: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("shadow state must be an object")
        return value

    def _logged_signal_ids(self) -> set[str]:
        if not self.log_path.exists():
            return set()
        values: set[str] = set()
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"cannot read shadow log: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                signal = StrategySignal.from_dict(payload["signal"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid shadow log line {line_number}: {exc}") from exc
            values.add(str(payload.get("decisionId") or self._signal_id(signal)))
        return values

    @staticmethod
    def load_signal(log_path: Path, signal_id: str | None = None) -> StrategySignal:
        if not log_path.exists():
            raise ValueError(f"shadow log does not exist: {log_path}")
        accepted: list[tuple[str, StrategySignal]] = []
        for line_number, line in enumerate(
            log_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if payload.get("event") != "SHADOW_SIGNAL":
                    continue
                if payload.get("decision") != "ACCEPTED":
                    continue
                signal = StrategySignal.from_dict(payload["signal"])
                candidate_id = str(payload["signalId"])
                if candidate_id != signal.signal_id:
                    raise ValueError("shadow signal ID does not match signal content")
                accepted.append((candidate_id, signal))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid shadow log line {line_number}: {exc}") from exc
        if signal_id is not None:
            for candidate_id, signal in accepted:
                if candidate_id == signal_id:
                    return signal
            raise ValueError(f"accepted shadow signal not found: {signal_id}")
        if not accepted:
            raise ValueError("shadow log contains no accepted signal")
        return accepted[-1][1]

    @staticmethod
    def load_decision(
        log_path: Path, decision_id: str | None = None
    ) -> StrategyDecision:
        if not log_path.exists():
            raise ValueError(f"shadow log does not exist: {log_path}")
        accepted: list[tuple[str, StrategyDecision]] = []
        for line_number, line in enumerate(
            log_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if payload.get("event") != "SHADOW_DECISION":
                    continue
                if payload.get("decision") not in {"ACCEPTED", "REVERSE_ACCEPTED"}:
                    continue
                raw = payload["strategyDecision"]
                if not isinstance(raw, dict):
                    raise ValueError("strategy decision payload must be an object")
                value = StrategyDecision.from_dict(raw)
                candidate_id = str(payload["decisionId"])
                if candidate_id != value.decision_id:
                    raise ValueError("shadow decision ID does not match decision content")
                accepted.append((candidate_id, value))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid shadow log line {line_number}: {exc}") from exc
        if decision_id is not None:
            for candidate_id, value in accepted:
                if candidate_id == decision_id:
                    return value
            raise ValueError(f"accepted shadow decision not found: {decision_id}")
        if not accepted:
            raise ValueError("shadow log contains no accepted strategy decision")
        return accepted[-1][1]

    @staticmethod
    def _signal_id(signal: StrategySignal) -> str:
        return signal.signal_id

    def _write_state(self, value: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)
