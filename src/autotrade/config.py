from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import ConfigurationError


TESTNET_REST_URL = "https://demo-fapi.binance.com"
TESTNET_WS_URL = "wss://fstream.binancefuture.com"
MAINNET_REST_URL = "https://fapi.binance.com"
MAINNET_WS_URL = "wss://fstream.binance.com"


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise ConfigurationError(f"{name} must be a decimal number") from exc


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class RiskSettings:
    max_risk_usdt: Decimal
    max_risk_fraction: Decimal
    max_order_notional: Decimal
    max_symbol_notional: Decimal
    max_total_notional: Decimal
    max_leverage: int
    max_open_symbols: int
    max_daily_loss: Decimal
    max_consecutive_losses: int
    min_available_margin: Decimal
    min_liquidation_distance: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    max_mark_age_seconds: int

    @classmethod
    def from_env(cls) -> "RiskSettings":
        values = cls(
            max_risk_usdt=_decimal_env("AUTOTRADE_MAX_RISK_USDT", "25"),
            max_risk_fraction=_decimal_env("AUTOTRADE_MAX_RISK_FRACTION", "0.01"),
            max_order_notional=_decimal_env("AUTOTRADE_MAX_ORDER_NOTIONAL", "2500"),
            max_symbol_notional=_decimal_env("AUTOTRADE_MAX_SYMBOL_NOTIONAL", "2500"),
            max_total_notional=_decimal_env("AUTOTRADE_MAX_TOTAL_NOTIONAL", "5000"),
            max_leverage=_int_env("AUTOTRADE_MAX_LEVERAGE", 5),
            max_open_symbols=_int_env("AUTOTRADE_MAX_OPEN_SYMBOLS", 3),
            max_daily_loss=_decimal_env("AUTOTRADE_MAX_DAILY_LOSS", "100"),
            max_consecutive_losses=_int_env("AUTOTRADE_MAX_CONSECUTIVE_LOSSES", 3),
            min_available_margin=_decimal_env("AUTOTRADE_MIN_AVAILABLE_MARGIN", "50"),
            min_liquidation_distance=_decimal_env(
                "AUTOTRADE_MIN_LIQUIDATION_DISTANCE", "0.10"
            ),
            fee_bps=_decimal_env("AUTOTRADE_FEE_BPS", "5"),
            slippage_bps=_decimal_env("AUTOTRADE_SLIPPAGE_BPS", "10"),
            max_mark_age_seconds=_int_env("AUTOTRADE_MAX_MARK_AGE_SECONDS", 10),
        )
        if values.max_risk_usdt <= 0 or not Decimal("0") < values.max_risk_fraction <= 1:
            raise ConfigurationError("risk limits must be positive and fraction must be <= 1")
        if values.max_leverage < 1 or values.max_open_symbols < 1:
            raise ConfigurationError("leverage and open-symbol limits must be positive")
        return values


def load_env_file(path: str | Path = ".env") -> None:
    """Load a small KEY=VALUE env file without overriding process variables."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    rest_url: str
    ws_url: str
    api_key: str
    api_secret: str
    recv_window_ms: int
    database_path: Path
    log_path: Path
    lock_path: Path
    risk: RiskSettings
    unprotected_action: str
    strategy_config_path: Path
    strategy_state_dir: Path

    @property
    def is_testnet(self) -> bool:
        return self.environment == "testnet"

    @classmethod
    def from_env(cls, *, require_credentials: bool = False) -> "Settings":
        load_env_file()
        environment = os.getenv("BINANCE_ENV", "testnet").strip().lower()
        if environment not in {"testnet", "mainnet"}:
            raise ConfigurationError("BINANCE_ENV must be 'testnet' or 'mainnet'")

        if environment == "mainnet":
            if os.getenv("BINANCE_ALLOW_MAINNET") != "I_UNDERSTAND":
                raise ConfigurationError(
                    "Mainnet is locked. Set BINANCE_ALLOW_MAINNET=I_UNDERSTAND explicitly."
                )
            rest_url, ws_url = MAINNET_REST_URL, MAINNET_WS_URL
        else:
            rest_url, ws_url = TESTNET_REST_URL, TESTNET_WS_URL

        api_key = os.getenv("BINANCE_API_KEY", "").strip()
        api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        if require_credentials and (not api_key or not api_secret):
            raise ConfigurationError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required for this command"
            )

        try:
            recv_window_ms = int(os.getenv("AUTOTRADE_RECV_WINDOW_MS", "5000"))
        except ValueError as exc:
            raise ConfigurationError("AUTOTRADE_RECV_WINDOW_MS must be an integer") from exc
        if not 1 <= recv_window_ms <= 60_000:
            raise ConfigurationError("AUTOTRADE_RECV_WINDOW_MS must be between 1 and 60000")

        unprotected_action = os.getenv("AUTOTRADE_UNPROTECTED_ACTION", "pause").lower()
        if unprotected_action not in {"pause", "close"}:
            raise ConfigurationError("AUTOTRADE_UNPROTECTED_ACTION must be pause or close")

        return cls(
            environment=environment,
            rest_url=rest_url,
            ws_url=ws_url,
            api_key=api_key,
            api_secret=api_secret,
            recv_window_ms=recv_window_ms,
            database_path=Path(os.getenv("AUTOTRADE_DB", ".autotrade/orders.db")),
            log_path=Path(os.getenv("AUTOTRADE_LOG", ".autotrade/autotrade.jsonl")),
            lock_path=Path(os.getenv("AUTOTRADE_LOCK", ".autotrade/writer.lock")),
            risk=RiskSettings.from_env(),
            unprotected_action=unprotected_action,
            strategy_config_path=Path(
                os.getenv("AUTOTRADE_STRATEGY_CONFIG", "strategies.toml")
            ),
            strategy_state_dir=Path(
                os.getenv("AUTOTRADE_STRATEGY_STATE_DIR", ".autotrade/strategies")
            ),
        )
