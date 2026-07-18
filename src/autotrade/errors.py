from __future__ import annotations

from dataclasses import dataclass, field


class AutoTradeError(Exception):
    """Base application error."""


class ConfigurationError(AutoTradeError):
    """Raised when runtime configuration is unsafe or incomplete."""


class RuleViolation(AutoTradeError):
    """Raised when an order violates exchange filters or local risk rules."""


class RiskRejected(RuleViolation):
    """Raised when the account-level risk governor rejects an entry."""


class EntryPaused(RiskRejected):
    """Raised when new entries are administratively or automatically paused."""


class ReconciliationError(AutoTradeError):
    """Raised when local and exchange state cannot be reconciled safely."""


class InstanceLockError(AutoTradeError):
    """Raised when another writer process already owns the account lock."""


class LocalRateLimitExceeded(AutoTradeError):
    """Raised before a non-risk request would exhaust the local API budget."""


@dataclass(slots=True)
class BinanceAPIError(AutoTradeError):
    status_code: int
    code: int | None
    message: str
    rate_limits: dict[str, str] = field(default_factory=dict)
    execution_unknown: bool = False

    def __str__(self) -> str:
        code = f" code={self.code}" if self.code is not None else ""
        unknown = " execution_unknown=true" if self.execution_unknown else ""
        return f"Binance API error status={self.status_code}{code}{unknown}: {self.message}"


class ProtectionError(AutoTradeError):
    """Raised when an open position could not be protected."""
