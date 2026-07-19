from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


TERMINAL_ORDER_STATES = {
    "FILLED",
    "CANCELED",
    "EXPIRED",
    "REJECTED",
    "FINISHED",
}
ORDER_STATE_RANK = {
    "CREATED": 0,
    "SUBMITTING": 1,
    "UNKNOWN": 2,
    "NEW": 3,
    "PARTIALLY_FILLED": 4,
    "TRIGGERING": 4,
    "TRIGGERED": 4,
    "FILLED": 5,
    "FINISHED": 5,
    "CANCELED": 5,
    "EXPIRED": 5,
    "REJECTED": 5,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


class OrderJournal:
    """SQLite write-ahead journal for intents, orders, fills and control state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trade_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity TEXT NOT NULL,
            stop_price TEXT NOT NULL,
            take_profit_price TEXT,
            status TEXT NOT NULL,
            entry_order_id TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent_id INTEGER,
            symbol TEXT NOT NULL,
            client_order_id TEXT NOT NULL UNIQUE,
            exchange_order_id TEXT,
            family TEXT NOT NULL,
            role TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            status TEXT NOT NULL,
            original_quantity TEXT NOT NULL DEFAULT '0',
            executed_quantity TEXT NOT NULL DEFAULT '0',
            average_price TEXT NOT NULL DEFAULT '0',
            trigger_price TEXT,
            reduce_only INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(intent_id) REFERENCES trade_intents(id)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders(symbol, status);
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_order_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT,
            exchange_time INTEGER,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(client_order_id, event_type, status, exchange_time)
        );
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            client_order_id TEXT,
            exchange_order_id TEXT,
            side TEXT,
            quantity TEXT NOT NULL,
            price TEXT NOT NULL,
            commission TEXT NOT NULL DEFAULT '0',
            commission_asset TEXT,
            realized_pnl TEXT NOT NULL DEFAULT '0',
            exchange_time INTEGER,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(symbol, trade_id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            event_type TEXT NOT NULL,
            symbol TEXT,
            correlation_id TEXT,
            severity TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS control_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            reason TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_balance TEXT NOT NULL,
            available_balance TEXT NOT NULL,
            unrealized_pnl TEXT NOT NULL,
            maintenance_margin TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            close_time INTEGER NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            volume TEXT NOT NULL,
            trade_count INTEGER NOT NULL,
            closed INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(symbol, interval, open_time)
        );
        CREATE TABLE IF NOT EXISTS operator_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_operator_commands_status
            ON operator_commands(status, id);
        CREATE TABLE IF NOT EXISTS strategy_submissions (
            signal_id TEXT PRIMARY KEY,
            command_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(command_id) REFERENCES operator_commands(id)
        );
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            if self._connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                self._connection.execute("INSERT INTO schema_version(version) VALUES (2)")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO control_state(key, value, reason, updated_at)
                VALUES ('entry_enabled', 'false', 'default deny until operator review', ?)
                """,
                (utc_now(),),
            )

    def close(self) -> None:
        self._connection.close()

    def create_intent(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: str,
        stop_price: str,
        take_profit_price: str | None,
        details: dict[str, Any],
    ) -> int:
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO trade_intents (
                    client_order_id, symbol, side, quantity, stop_price,
                    take_profit_price, status, details_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)
                """,
                (
                    client_order_id,
                    symbol,
                    side,
                    quantity,
                    stop_price,
                    take_profit_price,
                    _json(details),
                    now,
                    now,
                ),
            )
            intent_id = int(cursor.lastrowid)
            self._append_audit_locked(
                "intent", "CREATED", symbol, client_order_id, "INFO", details
            )
        return intent_id

    def update(
        self,
        intent_id: int,
        status: str,
        *,
        entry_order_id: str | int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT symbol, client_order_id FROM trade_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            values: list[Any] = [status, utc_now()]
            assignments = ["status = ?", "updated_at = ?"]
            if entry_order_id is not None:
                assignments.append("entry_order_id = ?")
                values.append(str(entry_order_id))
            if details is not None:
                assignments.append("details_json = ?")
                values.append(_json(details))
            values.append(intent_id)
            self._connection.execute(
                f"UPDATE trade_intents SET {', '.join(assignments)} WHERE id = ?", values
            )
            if row:
                self._append_audit_locked(
                    "intent", status, row["symbol"], row["client_order_id"], "INFO", details or {}
                )

    def record_order(
        self,
        payload: dict[str, Any],
        *,
        family: str,
        role: str,
        intent_id: int | None = None,
    ) -> None:
        client_order_id = str(payload.get("clientOrderId") or payload.get("clientAlgoId") or "")
        if not client_order_id:
            raise ValueError("order payload has no client order id")
        now = utc_now()
        values = (
            intent_id,
            str(payload.get("symbol", "")),
            client_order_id,
            str(payload.get("orderId") or payload.get("algoId") or "") or None,
            family,
            role,
            str(payload.get("side", "")),
            str(payload.get("type") or payload.get("orderType") or ""),
            str(payload.get("status") or payload.get("algoStatus") or "NEW"),
            str(payload.get("origQty") or payload.get("quantity") or "0"),
            str(payload.get("executedQty") or payload.get("cumQty") or "0"),
            str(payload.get("avgPrice") or "0"),
            payload.get("stopPrice") or payload.get("triggerPrice"),
            int(bool(payload.get("reduceOnly"))),
            _json(payload),
            now,
            now,
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO orders (
                    intent_id, symbol, client_order_id, exchange_order_id, family, role,
                    side, order_type, status, original_quantity, executed_quantity,
                    average_price, trigger_price, reduce_only, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id=excluded.exchange_order_id,
                    family=excluded.family,
                    role=excluded.role,
                    side=excluded.side,
                    order_type=CASE
                        WHEN excluded.order_type = 'MARKET'
                             AND orders.order_type IN ('STOP_MARKET','TAKE_PROFIT_MARKET')
                        THEN orders.order_type
                        ELSE excluded.order_type END,
                    reduce_only=excluded.reduce_only,
                    status=CASE
                        WHEN orders.status IN ('FILLED','CANCELED','EXPIRED','REJECTED','FINISHED')
                        THEN orders.status ELSE excluded.status END,
                    original_quantity=excluded.original_quantity,
                    executed_quantity=excluded.executed_quantity,
                    average_price=excluded.average_price,
                    trigger_price=excluded.trigger_price,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            self._append_order_event_locked(
                client_order_id,
                "REST_RESPONSE",
                values[8],
                payload.get("updateTime") or payload.get("createTime"),
                payload,
            )

    def transition_order(
        self,
        client_order_id: str,
        status: str,
        *,
        event_type: str,
        exchange_time: int | None,
        payload: dict[str, Any],
        executed_quantity: str | None = None,
        average_price: str | None = None,
    ) -> bool:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            if row:
                current = str(row["status"])
                if current in TERMINAL_ORDER_STATES and status != current:
                    # Binance may emit an Algo FINISHED event after its execution
                    # order is reported as FILLED, but a later FILLED event must
                    # still be allowed to complete the local execution record.
                    if not (current == "FINISHED" and status == "FILLED"):
                        self._append_order_event_locked(
                            client_order_id, "IGNORED_STALE_EVENT", status, exchange_time, payload
                        )
                        return False
                if ORDER_STATE_RANK.get(status, 0) < ORDER_STATE_RANK.get(current, 0):
                    self._append_order_event_locked(
                        client_order_id, "IGNORED_STALE_EVENT", status, exchange_time, payload
                    )
                    return False
                assignments = ["status = ?", "payload_json = ?", "updated_at = ?"]
                values: list[Any] = [status, _json(payload), utc_now()]
                if executed_quantity is not None:
                    assignments.append("executed_quantity = ?")
                    values.append(executed_quantity)
                if average_price is not None:
                    assignments.append("average_price = ?")
                    values.append(average_price)
                values.append(client_order_id)
                self._connection.execute(
                    f"UPDATE orders SET {', '.join(assignments)} WHERE client_order_id = ?", values
                )
            self._append_order_event_locked(
                client_order_id, event_type, status, exchange_time, payload
            )
            return True

    def _append_order_event_locked(
        self,
        client_order_id: str,
        event_type: str,
        status: str | None,
        exchange_time: int | None,
        payload: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO order_events (
                client_order_id, event_type, status, exchange_time, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (client_order_id, event_type, status, exchange_time or 0, _json(payload), utc_now()),
        )

    def record_fill(self, payload: dict[str, Any]) -> None:
        symbol = str(payload.get("s") or payload.get("symbol") or "")
        trade_id = str(payload.get("t") or payload.get("tradeId") or payload.get("id") or "")
        if not symbol or not trade_id or trade_id == "0":
            return
        client_order_id = payload.get("c") or payload.get("clientOrderId")
        exchange_order_id = str(payload.get("i") or payload.get("orderId") or "")
        side = payload.get("S") or payload.get("side")
        quantity = str(payload.get("l") or payload.get("qty") or "0")
        price = str(payload.get("L") or payload.get("price") or "0")
        commission = str(payload.get("n") or payload.get("commission") or "0")
        commission_asset = payload.get("N") or payload.get("commissionAsset")
        realized_pnl = str(payload.get("rp") or payload.get("realizedPnl") or "0")
        exchange_time = payload.get("T") or payload.get("time")
        rich_fill = any(
            key in payload for key in ("n", "N", "rp", "commission", "commissionAsset", "realizedPnl")
        )
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO fills (
                    symbol, trade_id, client_order_id, exchange_order_id, side,
                    quantity, price, commission, commission_asset, realized_pnl,
                    exchange_time, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    trade_id,
                    client_order_id,
                    exchange_order_id,
                    side,
                    quantity,
                    price,
                    commission,
                    commission_asset,
                    realized_pnl,
                    exchange_time,
                    _json(payload),
                    utc_now(),
                ),
            )
            if cursor.rowcount == 0 and rich_fill:
                # TRADE_LITE can precede ORDER_TRADE_UPDATE. Enrich the existing
                # lightweight row without allowing a later lightweight duplicate
                # to erase commission or realized PnL.
                self._connection.execute(
                    """
                    UPDATE fills SET
                        client_order_id=?, exchange_order_id=?, side=?, quantity=?, price=?,
                        commission=?, commission_asset=?, realized_pnl=?, exchange_time=?,
                        payload_json=?, created_at=created_at
                    WHERE symbol=? AND trade_id=?
                    """,
                    (
                        client_order_id,
                        exchange_order_id,
                        side,
                        quantity,
                        price,
                        commission,
                        commission_asset,
                        realized_pnl,
                        exchange_time,
                        _json(payload),
                        symbol,
                        trade_id,
                    ),
                )

    def append_audit(
        self,
        category: str,
        event_type: str,
        *,
        symbol: str | None = None,
        correlation_id: str | None = None,
        severity: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._append_audit_locked(
                category, event_type, symbol, correlation_id, severity, payload or {}
            )

    def _append_audit_locked(
        self,
        category: str,
        event_type: str,
        symbol: str | None,
        correlation_id: str | None,
        severity: str,
        payload: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO audit_events (
                category, event_type, symbol, correlation_id, severity, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (category, event_type, symbol, correlation_id, severity, _json(payload), utc_now()),
        )

    def set_control(
        self,
        key: str,
        value: str,
        reason: str | None = None,
        *,
        severity: str = "WARNING",
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO control_state(key, value, reason, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, reason=excluded.reason, updated_at=excluded.updated_at
                """,
                (key, value, reason, utc_now()),
            )
            self._append_audit_locked(
                "control", key, None, None, severity.upper(), {"value": value, "reason": reason}
            )

    def get_control(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM control_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def control_snapshot(self) -> dict[str, dict[str, str | None]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM control_state").fetchall()
        return {
            row["key"]: {
                "value": row["value"],
                "reason": row["reason"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def record_account_snapshot(self, payload: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO account_snapshots (
                    wallet_balance, available_balance, unrealized_pnl,
                    maintenance_margin, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("totalWalletBalance", "0")),
                    str(payload.get("availableBalance", "0")),
                    str(payload.get("totalUnrealizedProfit", "0")),
                    str(payload.get("totalMaintMargin", "0")),
                    _json(payload),
                    utc_now(),
                ),
            )

    def store_candle(self, candle: dict[str, Any]) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO candles (
                    symbol, interval, open_time, close_time, open, high, low, close,
                    volume, trade_count, closed, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candle["symbol"],
                    candle["interval"],
                    candle["open_time"],
                    candle["close_time"],
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"],
                    candle["volume"],
                    candle["trade_count"],
                    int(bool(candle["closed"])),
                    _json(candle),
                    utc_now(),
                ),
            )
        return cursor.rowcount > 0

    def latest_candle_open_time(self, symbol: str, interval: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(open_time) AS value FROM candles WHERE symbol=? AND interval=?",
                (symbol, interval),
            ).fetchone()
        return int(row["value"]) if row and row["value"] is not None else None

    def candles(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT symbol, interval, open_time, close_time, open, high, low, close,
                   volume, trade_count, closed
            FROM candles
            WHERE symbol = ? AND interval = ? AND closed = 1
        """
        params: list[Any] = [symbol.upper(), interval]
        if start_time is not None:
            query += " AND open_time >= ?"
            params.append(start_time)
        if end_time is not None:
            query += " AND open_time < ?"
            params.append(end_time)
        query += " ORDER BY open_time"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["closed"] = bool(value["closed"])
        return values

    def active_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM orders WHERE status NOT IN ('FILLED','CANCELED','EXPIRED','REJECTED')"
        params: tuple[Any, ...] = ()
        if symbol:
            query += " AND symbol = ?"
            params = (symbol.upper(),)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY id", params).fetchall()
        return [dict(row) for row in rows]

    def order(self, client_order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return dict(row) if row else None

    def order_events(self, client_order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM order_events WHERE client_order_id = ? ORDER BY id",
                (client_order_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM trade_intents ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_intent(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM trade_intents WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol.upper(),),
            ).fetchone()
        return dict(row) if row else None

    def latest_active_intent(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM trade_intents
                WHERE symbol = ? AND status NOT IN ('CLOSED','REJECTED','EMERGENCY_CLOSED')
                ORDER BY id DESC LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return dict(row) if row else None

    def close_active_intents(self, symbol: str, reason: str) -> int:
        now = utc_now()
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT id, client_order_id FROM trade_intents
                WHERE symbol = ? AND status NOT IN ('CLOSED','REJECTED','EMERGENCY_CLOSED')
                """,
                (symbol.upper(),),
            ).fetchall()
            self._connection.execute(
                """
                UPDATE trade_intents SET status='CLOSED', updated_at=?
                WHERE symbol=? AND status NOT IN ('CLOSED','REJECTED','EMERGENCY_CLOSED')
                """,
                (now, symbol.upper()),
            )
            for row in rows:
                self._append_audit_locked(
                    "intent",
                    "CLOSED",
                    symbol.upper(),
                    row["client_order_id"],
                    "INFO",
                    {"reason": reason},
                )
        return len(rows)

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_local_pnl(self, day_prefix: str | None = None) -> Decimal:
        prefix = day_prefix or datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            rows = self._connection.execute(
                "SELECT realized_pnl, commission FROM fills WHERE created_at LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        return sum(
            (Decimal(row["realized_pnl"]) - abs(Decimal(row["commission"])) for row in rows),
            Decimal("0"),
        )

    def enqueue_command(self, command_type: str, payload: dict[str, Any]) -> int:
        now = utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO operator_commands (
                    command_type, payload_json, status, created_at, updated_at
                ) VALUES (?, ?, 'PENDING', ?, ?)
                """,
                (command_type, _json(payload), now, now),
            )
            command_id = int(cursor.lastrowid)
            self._append_audit_locked(
                "operator_command",
                "QUEUED",
                payload.get("symbol"),
                str(command_id),
                "WARNING",
                {"command_type": command_type, **payload},
            )
        return command_id

    def pending_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM operator_commands
                WHERE status='PENDING' ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def strategy_signal_command_exists(self, signal_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM strategy_submissions WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return row is not None

    def pending_entry_command_exists(self) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM operator_commands
                WHERE command_type = 'ENTRY_INTENT'
                  AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def enqueue_strategy_signal(
        self, signal_id: str, payload: dict[str, Any]
    ) -> int | None:
        now = utc_now()
        with self._lock, self._connection:
            if self._connection.execute(
                """
                SELECT 1 FROM operator_commands
                WHERE command_type = 'ENTRY_INTENT'
                  AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """
            ).fetchone():
                return None
            if self._connection.execute(
                "SELECT 1 FROM strategy_submissions WHERE signal_id = ?", (signal_id,)
            ).fetchone():
                return None
            cursor = self._connection.execute(
                """
                INSERT INTO operator_commands (
                    command_type, payload_json, status, created_at, updated_at
                ) VALUES ('ENTRY_INTENT', ?, 'PENDING', ?, ?)
                """,
                (_json(payload), now, now),
            )
            command_id = int(cursor.lastrowid)
            self._connection.execute(
                """
                INSERT INTO strategy_submissions(signal_id, command_id, created_at)
                VALUES (?, ?, ?)
                """,
                (signal_id, command_id, now),
            )
            self._append_audit_locked(
                "operator_command",
                "QUEUED",
                payload.get("symbol"),
                str(command_id),
                "WARNING",
                {"command_type": "ENTRY_INTENT", "signal_id": signal_id, **payload},
            )
        return command_id

    def mark_command_running(self, command_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE operator_commands SET status='RUNNING', updated_at=?
                WHERE id=? AND status='PENDING'
                """,
                (utc_now(), command_id),
            )
        return cursor.rowcount == 1

    def complete_command(
        self, command_id: int, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        status = "FAILED" if error else "COMPLETED"
        payload = {"error": error} if error else (result or {})
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE operator_commands
                SET status=?, result_json=?, updated_at=? WHERE id=?
                """,
                (status, _json(payload), utc_now(), command_id),
            )
            self._append_audit_locked(
                "operator_command",
                status,
                None,
                str(command_id),
                "ERROR" if error else "INFO",
                payload,
            )

    def recent_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM operator_commands ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
