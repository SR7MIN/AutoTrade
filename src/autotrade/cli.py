from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .backtest import BacktestEngine
from .binance_rest import BinanceRestClient
from .candles import Candle
from .config import Settings
from .daemon import AccountReconciler, TradingDaemon
from .errors import AutoTradeError, InstanceLockError, RuleViolation
from .journal import OrderJournal
from .intents import EntryIntent
from .locking import SingleInstanceLock, lock_owner_active
from .market_data import MarketDataService
from .observability import AlertManager, configure_logging
from .risk_control import RiskGovernor, utc_day_start_ms
from .rules import SymbolRules, decimal_value
from .shadow import ShadowRunner
from .strategy import EmaAtrStrategy, build_strategy
from .strategy_adapter import TestnetStrategyAdapter
from .strategy_manager import StrategyManager
from .trading import TradingService
from .user_stream import stream_user_events


def decimal_argument(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc


def timestamp_argument(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "timestamp must be epoch milliseconds or an ISO-8601 date/time"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str), file=stream)


def load_strategy_manager(settings: Settings, args: argparse.Namespace) -> StrategyManager:
    config_path = getattr(args, "config", None) or settings.strategy_config_path
    return StrategyManager.from_toml(
        config_path, state_root=settings.strategy_state_dir
    )


def add_trade_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", required=True, help="USD-M symbol, for example BTCUSDT")
    parser.add_argument("--side", required=True, choices=("BUY", "SELL"))
    parser.add_argument("--risk-usdt", required=True, type=decimal_argument)
    parser.add_argument("--stop-price", required=True, type=decimal_argument)
    parser.add_argument("--take-profit", type=decimal_argument)
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument(
        "--margin-utilization",
        type=decimal_argument,
        default=Decimal("0.50"),
        help="maximum fraction of available margin used by this trade (default: 0.50)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotrade", description="Testnet-first Binance USD-M Futures trading core"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="check public API connectivity and exchange limits")

    quote = subparsers.add_parser("quote", help="show mark price and active symbol filters")
    quote.add_argument("--symbol", required=True)

    preview = subparsers.add_parser("preview", help="calculate a public-data trade plan")
    add_trade_arguments(preview)
    preview.add_argument("--available-margin", type=decimal_argument)

    bracket = subparsers.add_parser(
        "bracket", help="preview or execute a market entry with exchange-side protection"
    )
    add_trade_arguments(bracket)
    bracket.add_argument(
        "--execute", action="store_true", help="send orders; without this flag only preview"
    )

    subparsers.add_parser("account", help="show balances, positions and open order counts")

    cancel = subparsers.add_parser("cancel-all", help="cancel ordinary and algo orders")
    cancel.add_argument("--symbol", required=True)
    cancel.add_argument("--execute", action="store_true", help="required to send cancellations")

    take_profit = subparsers.add_parser(
        "take-profit", help="preview or add take-profit protection to an existing position"
    )
    take_profit.add_argument("--symbol", required=True)
    take_profit.add_argument("--trigger-price", required=True, type=decimal_argument)
    take_profit.add_argument("--execute", action="store_true", help="send the Algo order")

    close = subparsers.add_parser("close-position", help="preview or reduce an open position")
    close.add_argument("--symbol", required=True)
    close.add_argument("--quantity", type=decimal_argument)
    close.add_argument("--execute", action="store_true")

    replace_stop = subparsers.add_parser("replace-stop", help="safely replace stop protection")
    replace_stop.add_argument("--symbol", required=True)
    replace_stop.add_argument("--trigger-price", required=True, type=decimal_argument)
    replace_stop.add_argument("--execute", action="store_true")

    replace_tp = subparsers.add_parser(
        "replace-take-profit", help="safely replace take-profit protection"
    )
    replace_tp.add_argument("--symbol", required=True)
    replace_tp.add_argument("--trigger-price", required=True, type=decimal_argument)
    replace_tp.add_argument("--execute", action="store_true")

    protect = subparsers.add_parser(
        "protect-position", help="attach or replace protection on an existing position"
    )
    protect.add_argument("--symbol", required=True)
    protect.add_argument("--stop-price", required=True, type=decimal_argument)
    protect.add_argument("--take-profit", type=decimal_argument)
    protect.add_argument("--execute", action="store_true")

    subparsers.add_parser("stream", help="print private user-data events as JSON lines")

    watch = subparsers.add_parser(
        "watch", help="diagnostic read-only user-event stream"
    )
    watch.add_argument("--symbol", required=True)

    daemon = subparsers.add_parser("daemon", help="run reconciliation and market-data services")
    daemon.add_argument("--symbols", nargs="+", required=True)
    daemon.add_argument("--interval", default="1m")

    reconcile = subparsers.add_parser("reconcile", help="run a full REST reconciliation")
    reconcile.add_argument("--symbols", nargs="+", required=True)
    reconcile.add_argument("--execute", action="store_true")

    pause = subparsers.add_parser("pause-entry", help="persistently disable new entries")
    pause.add_argument("--reason", required=True)
    resume = subparsers.add_parser("resume-entry", help="explicitly re-enable new entries")
    resume.add_argument("--reason", required=True)

    kill = subparsers.add_parser("kill-switch", help="pause entries and cancel or close exposure")
    kill.add_argument("--symbols", nargs="+", required=True)
    kill.add_argument("--close-positions", action="store_true")
    kill.add_argument("--reason", required=True)
    kill.add_argument("--execute", action="store_true")

    subparsers.add_parser("snapshot", help="show exchange, local control and rate-limit state")
    income = subparsers.add_parser("income", help="show today's relevant futures income")
    income.add_argument("--limit", type=int, default=100)
    audit = subparsers.add_parser("audit", help="show recent local audit events")
    audit.add_argument("--limit", type=int, default=50)
    commands = subparsers.add_parser("commands", help="show recent operator commands")
    commands.add_argument("--limit", type=int, default=20)

    backfill = subparsers.add_parser("backfill", help="backfill closed klines into SQLite")
    backfill.add_argument("--symbol", required=True)
    backfill.add_argument("--interval", default="1m")

    backfill_range = subparsers.add_parser(
        "backfill-range", help="page through a closed-kline UTC range into a research database"
    )
    backfill_range.add_argument("--symbol", required=True)
    backfill_range.add_argument("--interval", default="5m")
    backfill_range.add_argument("--start", required=True, type=timestamp_argument)
    backfill_range.add_argument("--end", required=True, type=timestamp_argument)
    backfill_range.add_argument("--page-limit", type=int, default=1000)
    backfill_range.add_argument(
        "--database", type=Path, default=Path(".autotrade/research.db")
    )

    replay = subparsers.add_parser(
        "replay-strategy", help="replay a deterministic strategy over stored closed klines"
    )
    replay.add_argument("--strategy")
    replay.add_argument("--instance")
    replay.add_argument("--config", type=Path)
    replay.add_argument("--symbol")
    replay.add_argument("--interval")
    replay.add_argument("--start", type=timestamp_argument)
    replay.add_argument("--end", type=timestamp_argument)
    replay.add_argument("--database", type=Path, default=Path(".autotrade/research.db"))
    replay.add_argument("--initial-balance", type=decimal_argument, default=Decimal("1000"))
    replay.add_argument("--fee-bps", type=decimal_argument, default=Decimal("5"))
    replay.add_argument("--slippage-bps", type=decimal_argument, default=Decimal("10"))
    replay.add_argument("--cooldown-bars", type=int)

    shadow = subparsers.add_parser(
        "shadow", help="run a read-only strategy against newly closed local candles"
    )
    shadow.add_argument("--strategy")
    shadow.add_argument("--instance")
    shadow.add_argument("--config", type=Path)
    shadow.add_argument("--symbol")
    shadow.add_argument("--interval")
    shadow.add_argument("--database", type=Path)
    shadow.add_argument("--state", type=Path)
    shadow.add_argument("--log", type=Path)
    shadow.add_argument("--poll-seconds", type=float, default=5.0)
    shadow.add_argument("--cooldown-bars", type=int)
    shadow.add_argument("--once", action="store_true")

    submit = subparsers.add_parser(
        "submit-strategy", help="preview or queue an accepted Shadow signal on Testnet"
    )
    submit.add_argument("--instance", required=True)
    submit.add_argument("--config", type=Path)
    submit.add_argument("--log", type=Path)
    submit.add_argument("--signal-id")
    submit.add_argument("--max-signal-age-seconds", type=int, default=90)
    submit.add_argument("--execute", action="store_true")
    submit.add_argument("--confirm-testnet", choices=("I_UNDERSTAND",))

    strategies = subparsers.add_parser(
        "strategies", help="list registered implementations and configured instances"
    )
    strategies.add_argument("--config", type=Path)

    activate = subparsers.add_parser(
        "activate-strategy", help="select the only strategy instance allowed to execute"
    )
    activate.add_argument("--instance", required=True)
    activate.add_argument("--config", type=Path)
    activate.add_argument("--reason", required=True)

    deactivate = subparsers.add_parser(
        "deactivate-strategy", help="disable strategy execution without changing entry pause"
    )
    deactivate.add_argument("--reason", required=True)

    journal = subparsers.add_parser("journal", help="show recent local trade intents")
    journal.add_argument("--limit", type=int, default=20)
    return parser


def credentials_required(args: argparse.Namespace) -> bool:
    if args.command in {
        "account", "stream", "watch", "daemon", "snapshot", "income",
        "take-profit", "close-position", "replace-stop", "replace-take-profit",
        "protect-position", "reconcile",
    }:
        return True
    return bool(args.command in {"bracket", "cancel-all", "kill-switch"} and args.execute)


def execute_or_enqueue(
    settings: Settings,
    journal: OrderJournal,
    command_type: str,
    payload: dict[str, Any],
    operation: Any,
) -> dict[str, Any]:
    if lock_owner_active(settings.lock_path):
        command_id = journal.enqueue_command(command_type, payload)
        return {"mode": "queued", "commandId": command_id, "command": command_type}
    if settings.lock_path.exists():
        raise InstanceLockError(
            f"Stale writer lock detected at {settings.lock_path}; verify the recorded PID before removal"
        )
    with SingleInstanceLock(settings.lock_path):
        return {"mode": "executed", "result": operation()}


def account_summary(client: BinanceRestClient) -> dict[str, Any]:
    client.sync_time()
    account = client.account()
    positions = [
        position
        for position in client.positions()
        if decimal_value(position.get("positionAmt", "0")) != 0
    ]
    return {
        "availableBalance": account.get("availableBalance"),
        "totalWalletBalance": account.get("totalWalletBalance"),
        "totalUnrealizedProfit": account.get("totalUnrealizedProfit"),
        "totalMaintMargin": account.get("totalMaintMargin"),
        "positions": positions,
        "openOrderCount": len(client.open_orders()),
        "openAlgoOrderCount": len(client.open_algo_orders()),
        "rateLimits": client.last_rate_limits,
    }


def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_credentials=credentials_required(args))
    if args.command in {"strategies", "activate-strategy", "deactivate-strategy"}:
        journal = OrderJournal(settings.database_path)
        try:
            if args.command == "deactivate-strategy":
                journal.set_control("active_strategy_instance", "", args.reason)
                print_json({"activeExecutionInstance": None, "reason": args.reason})
                return 0
            manager = load_strategy_manager(settings, args)
            if args.command == "strategies":
                active = journal.get_control("active_strategy_instance", "") or None
                print_json(manager.as_dict(active_instance=active))
                return 0
            instance = manager.instance(args.instance)
            journal.set_control(
                "active_strategy_instance", instance.instance_id, args.reason
            )
            print_json(
                {
                    "activeExecutionInstance": instance.instance_id,
                    "entryEnabled": journal.get_control("entry_enabled", "false") == "true",
                    "reason": args.reason,
                }
            )
            return 0
        finally:
            journal.close()
    if args.command in {"journal", "audit", "commands", "pause-entry", "resume-entry"}:
        journal = OrderJournal(settings.database_path)
        try:
            if args.command == "journal":
                print_json(journal.recent(args.limit))
            elif args.command == "audit":
                print_json(journal.recent_audit(args.limit))
            elif args.command == "commands":
                print_json(journal.recent_commands(args.limit))
            elif args.command == "pause-entry":
                journal.set_control("entry_enabled", "false", args.reason)
                print_json({"entryEnabled": False, "reason": args.reason})
            else:
                RiskGovernor(settings.risk, journal).unlock_entries(args.reason)
                print_json({"entryEnabled": True, "reason": args.reason})
        finally:
            journal.close()
        return 0

    if args.command == "daemon":
        asyncio.run(TradingDaemon(settings, args.symbols, args.interval).run())
        return 0

    if args.command == "replay-strategy":
        if args.instance:
            strategy = load_strategy_manager(settings, args).build(args.instance)
        else:
            strategy_name = args.strategy or EmaAtrStrategy.name
            if not args.symbol:
                raise ValueError("--symbol is required without --instance")
            strategy = build_strategy(
                strategy_name,
                symbol=args.symbol,
                interval=args.interval or "5m",
            )
        journal = OrderJournal(args.database)
        try:
            candles = [
                Candle.from_dict(value)
                for value in journal.candles(
                    strategy.symbol,
                    strategy.interval,
                    start_time=args.start,
                    end_time=args.end,
                )
            ]
        finally:
            journal.close()
        cooldown_bars = (
            args.cooldown_bars
            if args.cooldown_bars is not None
            else getattr(strategy, "cooldown_bars", 3)
        )
        result = BacktestEngine(
            initial_balance=args.initial_balance,
            fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
            cooldown_bars=cooldown_bars,
        ).run(candles, strategy)
        print_json({"database": str(args.database), **result.as_dict()})
        return 0

    if args.command == "shadow":
        if args.poll_seconds <= 0:
            raise ValueError("poll seconds must be positive")
        database = args.database or settings.database_path
        if args.instance:
            manager = load_strategy_manager(settings, args)
            strategy = manager.build(args.instance)
            paths = manager.paths(args.instance)
            state_path = args.state or paths.state
            log_path = args.log or paths.log
            shadow_lock = paths.lock
        else:
            strategy_name = args.strategy or EmaAtrStrategy.name
            if not args.symbol:
                raise ValueError("--symbol is required without --instance")
            strategy = build_strategy(
                strategy_name,
                symbol=args.symbol,
                interval=args.interval or "5m",
            )
            state_path = args.state or Path(".autotrade/shadow-state.json")
            log_path = args.log or Path(".autotrade/shadow.jsonl")
            shadow_lock = state_path.with_suffix(state_path.suffix + ".lock")
        cooldown_bars = (
            args.cooldown_bars
            if args.cooldown_bars is not None
            else getattr(strategy, "cooldown_bars", 3)
        )
        runner = ShadowRunner(
            database_path=database,
            state_path=state_path,
            log_path=log_path,
            cooldown_bars=cooldown_bars,
        )
        if shadow_lock.resolve() == settings.lock_path.resolve():
            raise ValueError("shadow lock path cannot equal the daemon writer lock")
        with SingleInstanceLock(shadow_lock):
            while True:
                print_json({"database": str(database), **runner.run_once(strategy).as_dict()})
                if args.once:
                    return 0
                try:
                    time.sleep(args.poll_seconds)
                except KeyboardInterrupt:
                    return 0

    if args.command == "submit-strategy":
        if args.execute and args.confirm_testnet != "I_UNDERSTAND":
            raise RuleViolation(
                "--execute requires --confirm-testnet I_UNDERSTAND"
            )
        manager = load_strategy_manager(settings, args)
        instance = manager.instance(args.instance)
        log_path = args.log or manager.paths(args.instance).log
        strategy_decision = None
        if instance.implementation == "multi-divergence-reversal-v1":
            strategy_decision = ShadowRunner.load_decision(log_path, args.signal_id)
            signal = (
                strategy_decision.entry_signal
                if hasattr(strategy_decision, "entry_signal")
                else None
            )
        else:
            signal = ShadowRunner.load_signal(log_path, args.signal_id)
        if signal is not None:
            if signal.instance_id != instance.instance_id:
                raise RuleViolation("Shadow signal does not belong to the selected instance")
            if (
                signal.strategy != instance.implementation
                or signal.symbol != instance.symbol
                or signal.interval != instance.interval
            ):
                raise RuleViolation("Shadow signal does not match configured strategy instance")
        journal = OrderJournal(settings.database_path)
        try:
            registration = manager.registry.registration(instance.implementation)
            result = TestnetStrategyAdapter(
                settings,
                journal,
                instance,
                registration.version,
                testnet_only=registration.testnet_only,
                research_only=registration.research_only,
            )
            if strategy_decision is not None:
                result = result.submit_decision(
                    strategy_decision,
                    execute=args.execute,
                    max_signal_age_seconds=args.max_signal_age_seconds,
                )
            else:
                result = result.submit(
                    signal,
                    execute=args.execute,
                    max_signal_age_seconds=args.max_signal_age_seconds,
                )
            print_json(result.as_dict())
        finally:
            journal.close()
        return 0

    with BinanceRestClient(settings) as client:
        if args.command == "health":
            offset = client.sync_time()
            exchange_info = client.exchange_info()
            print_json(
                {
                    "environment": settings.environment,
                    "restUrl": settings.rest_url,
                    "serverTime": client.server_time()["serverTime"],
                    "clockOffsetMs": offset,
                    "symbols": len(exchange_info.get("symbols", [])),
                    "exchangeRateLimits": exchange_info.get("rateLimits", []),
                    "observedRateLimits": client.last_rate_limits,
                }
            )
            return 0

        if args.command == "quote":
            rules = SymbolRules.from_exchange_info(client.exchange_info(), args.symbol)
            mark = client.mark_price(rules.symbol)
            print_json(
                {
                    "environment": settings.environment,
                    "symbol": rules.symbol,
                    "status": rules.status,
                    "markPrice": mark.get("markPrice"),
                    "indexPrice": mark.get("indexPrice"),
                    "lastFundingRate": mark.get("lastFundingRate"),
                    "tickSize": str(rules.tick_size),
                    "marketStepSize": str(rules.market_step_size),
                    "marketMinQty": str(rules.market_min_qty),
                    "marketMaxQty": str(rules.market_max_qty),
                    "minNotional": str(rules.min_notional),
                    "triggerProtect": str(rules.trigger_protect),
                }
            )
            return 0

        if args.command == "account":
            print_json(account_summary(client))
            return 0


        if args.command == "snapshot":
            journal = OrderJournal(settings.database_path)
            try:
                summary = account_summary(client)
                journal.record_account_snapshot(client.account())
                print_json(
                    {
                        "exchange": summary,
                        "controls": journal.control_snapshot(),
                        "activeLocalOrders": journal.active_orders(),
                        "rateLimits": client.rate_guard.as_dict(),
                    }
                )
            finally:
                journal.close()
            return 0

        if args.command == "income":
            values = client.income_history(start_time=utc_day_start_ms(), limit=args.limit)
            print_json(values)
            return 0

        if args.command == "stream":
            asyncio.run(stream_user_events(client, settings.ws_url, lambda event: print_json(event)))
            return 0


        if args.command == "watch":
            print_json(
                {
                    "warning": "watch is diagnostic and read-only; use daemon for reconciliation",
                    "symbol": args.symbol.upper(),
                }
            )
            asyncio.run(stream_user_events(client, settings.ws_url, lambda event: print_json(event)))
            return 0

        if args.command in {"backfill", "backfill-range"}:
            target = settings.database_path if args.command == "backfill" else args.database
            live_database = target.resolve() == settings.database_path.resolve()
            if live_database and lock_owner_active(settings.lock_path):
                raise InstanceLockError(
                    "daemon is active; backfill the separate research database instead"
                )
            if live_database and settings.lock_path.exists():
                raise InstanceLockError(
                    f"Stale writer lock detected at {settings.lock_path}; verify before removal"
                )
            lock = SingleInstanceLock(settings.lock_path) if live_database else nullcontext()
            with lock:
                journal = OrderJournal(target)
                try:
                    client.sync_time()
                    market = MarketDataService(client, journal, settings.ws_url)
                    if args.command == "backfill":
                        payload: dict[str, Any] = {
                            "inserted": market.backfill(args.symbol, args.interval),
                            "symbol": args.symbol.upper(),
                            "database": str(target),
                        }
                    else:
                        payload = {
                            "database": str(target),
                            **market.backfill_range(
                                args.symbol,
                                args.interval,
                                start_time=args.start,
                                end_time=args.end,
                                page_limit=args.page_limit,
                            ).as_dict(),
                        }
                    print_json(payload)
                finally:
                    journal.close()
            return 0

        journal = OrderJournal(settings.database_path)
        try:
            risk = RiskGovernor(settings.risk, journal)
            service = TradingService(client, journal, risk)
            if args.command in {"preview", "bracket"} and not (
                args.command == "bracket" and args.execute
            ):
                plan = service.preview(
                    symbol=args.symbol,
                    side=args.side,
                    risk_usdt=args.risk_usdt,
                    stop_price=args.stop_price,
                    take_profit_price=args.take_profit,
                    leverage=args.leverage,
                    available_margin=getattr(args, "available_margin", None),
                    margin_utilization=args.margin_utilization,
                )
                print_json(
                    {
                        "mode": "preview",
                        "environment": settings.environment,
                        "plan": plan.as_dict(),
                        "warning": "No order was sent",
                    }
                )
                return 0

            if args.command == "bracket":
                intent = EntryIntent.create(
                    source="manual-cli",
                    symbol=args.symbol,
                    side=args.side,
                    risk_usdt=args.risk_usdt,
                    stop_price=args.stop_price,
                    take_profit_price=args.take_profit,
                    leverage=args.leverage,
                    margin_utilization=args.margin_utilization,
                )
                if lock_owner_active(settings.lock_path):
                    command_id = journal.enqueue_command("ENTRY_INTENT", intent.as_dict())
                    print_json(
                        {
                            "mode": "queued",
                            "environment": settings.environment,
                            "commandId": command_id,
                            "intentId": intent.intent_id,
                            "expiresAtMs": intent.expires_at_ms,
                        }
                    )
                elif settings.lock_path.exists():
                    raise InstanceLockError(
                        f"Stale writer lock detected at {settings.lock_path}; verify before removal"
                    )
                else:
                    with SingleInstanceLock(settings.lock_path):
                        result = service.execute_intent(intent)
                    print_json(
                        {
                            "mode": "executed",
                            "environment": settings.environment,
                            **result.as_dict(),
                        }
                    )
                return 0

            if args.command == "cancel-all":
                if not args.execute:
                    print_json(
                        {
                            "mode": "preview",
                            "symbol": args.symbol.upper(),
                            "actions": ["cancel ordinary open orders", "cancel algo open orders"],
                            "warning": "No cancellation was sent",
                        }
                    )
                    return 0
                print_json(
                    execute_or_enqueue(
                        settings,
                        journal,
                        "CANCEL_ALL",
                        {"symbol": args.symbol.upper()},
                        lambda: service.cancel_all(args.symbol),
                    )
                )
                return 0
            if args.command == "take-profit":
                if args.execute:
                    print_json(
                        execute_or_enqueue(
                            settings,
                            journal,
                            "REPLACE_TAKE_PROFIT",
                            {"symbol": args.symbol.upper(), "price": str(args.trigger_price)},
                            lambda: service.replace_protection(
                                args.symbol, "TAKE_PROFIT_MARKET", args.trigger_price
                            ),
                        )
                    )
                else:
                    client.sync_time()
                    parameters = service.take_profit_parameters(
                        args.symbol, args.trigger_price
                    )
                    print_json(
                        {
                            "mode": "preview",
                            "parameters": parameters,
                            "warning": "No order was sent",
                        }
                    )
                return 0
            if args.command == "close-position":
                parameters = service.close_position_parameters(args.symbol, args.quantity)
                if not args.execute:
                    print_json({"mode": "preview", "parameters": parameters})
                else:
                    payload = {
                        "symbol": args.symbol.upper(),
                        "quantity": str(args.quantity) if args.quantity is not None else None,
                    }
                    print_json(
                        execute_or_enqueue(
                            settings,
                            journal,
                            "CLOSE_POSITION",
                            payload,
                            lambda: service.close_position(args.symbol, args.quantity),
                        )
                    )
                return 0
            if args.command in {"replace-stop", "replace-take-profit"}:
                order_type = (
                    "STOP_MARKET" if args.command == "replace-stop" else "TAKE_PROFIT_MARKET"
                )
                parameters = service.protection_parameters(
                    args.symbol, order_type, args.trigger_price
                )
                if not args.execute:
                    print_json({"mode": "preview", "parameters": parameters})
                else:
                    command_type = (
                        "REPLACE_STOP" if order_type == "STOP_MARKET" else "REPLACE_TAKE_PROFIT"
                    )
                    print_json(
                        execute_or_enqueue(
                            settings,
                            journal,
                            command_type,
                            {"symbol": args.symbol.upper(), "price": str(args.trigger_price)},
                            lambda: service.replace_protection(
                                args.symbol, order_type, args.trigger_price
                            ),
                        )
                    )
                return 0
            if args.command == "protect-position":
                preview = {
                    "stop": service.protection_parameters(
                        args.symbol, "STOP_MARKET", args.stop_price
                    ),
                    "takeProfit": (
                        service.protection_parameters(
                            args.symbol, "TAKE_PROFIT_MARKET", args.take_profit
                        )
                        if args.take_profit is not None
                        else None
                    ),
                }
                if not args.execute:
                    print_json({"mode": "preview", "parameters": preview})
                else:
                    payload = {
                        "symbol": args.symbol.upper(),
                        "stop_price": str(args.stop_price),
                        "take_profit_price": (
                            str(args.take_profit) if args.take_profit is not None else None
                        ),
                    }
                    print_json(
                        execute_or_enqueue(
                            settings,
                            journal,
                            "PROTECT_POSITION",
                            payload,
                            lambda: service.protect_position(
                                args.symbol,
                                stop_price=args.stop_price,
                                take_profit_price=args.take_profit,
                            ),
                        )
                    )
                return 0
            if args.command == "kill-switch":
                payload = {
                    "symbols": [value.upper() for value in args.symbols],
                    "close_positions": args.close_positions,
                    "reason": args.reason,
                }
                if not args.execute:
                    print_json({"mode": "preview", "command": "KILL_SWITCH", **payload})
                else:
                    def direct_kill() -> dict[str, Any]:
                        risk.lock_entries(args.reason)
                        results = {}
                        for symbol in args.symbols:
                            active = [
                                item for item in client.positions(symbol.upper())
                                if decimal_value(item.get("positionAmt", "0")) != 0
                            ]
                            if args.close_positions and active:
                                results[symbol.upper()] = service.close_position(symbol)
                            else:
                                results[symbol.upper()] = service.cancel_all(symbol)
                        return {"entryPaused": True, "results": results}

                    print_json(
                        execute_or_enqueue(
                            settings, journal, "KILL_SWITCH", payload, direct_kill
                        )
                    )
                return 0
            if args.command == "reconcile":
                if not args.execute:
                    print_json(
                        {
                            "mode": "preview",
                            "symbols": args.symbols,
                            "warning": "Reconciliation may repair or close unsafe exposure",
                        }
                    )
                    return 0
                with SingleInstanceLock(settings.lock_path):
                    logger = configure_logging(settings.log_path)
                    reconciler = AccountReconciler(
                        client,
                        journal,
                        service,
                        risk,
                        AlertManager(journal, logger),
                        args.symbols,
                        settings.unprotected_action,
                    )
                    print_json(reconciler.startup_reconcile())
                return 0
        finally:
            journal.close()
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except (AutoTradeError, ValueError, KeyError) as exc:
        print_json({"error": type(exc).__name__, "message": str(exc)}, stream=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
