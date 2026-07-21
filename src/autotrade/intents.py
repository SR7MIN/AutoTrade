from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from .errors import RuleViolation


@dataclass(frozen=True, slots=True)
class EntryIntent:
    """Strategy-facing immutable contract; contains no exchange client or credentials."""

    intent_id: str
    source: str
    symbol: str
    side: str
    risk_usdt: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    leverage: int
    margin_utilization: Decimal
    created_at_ms: int
    expires_at_ms: int
    min_stop_bps: Decimal | None = None
    max_stop_bps: Decimal | None = None

    @classmethod
    def create(
        cls,
        *,
        source: str,
        symbol: str,
        side: str,
        risk_usdt: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal | None,
        leverage: int,
        margin_utilization: Decimal = Decimal("0.50"),
        min_stop_bps: Decimal | None = None,
        max_stop_bps: Decimal | None = None,
        ttl_seconds: int = 30,
    ) -> "EntryIntent":
        now = int(time.time() * 1000)
        if not source.strip():
            raise RuleViolation("intent source is required")
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise RuleViolation("intent TTL must be between 1 and 300 seconds")
        return cls(
            intent_id=f"intent-{uuid.uuid4().hex}",
            source=source,
            symbol=symbol.upper(),
            side=side.upper(),
            risk_usdt=Decimal(risk_usdt),
            stop_price=Decimal(stop_price),
            take_profit_price=(Decimal(take_profit_price) if take_profit_price is not None else None),
            leverage=leverage,
            margin_utilization=Decimal(margin_utilization),
            created_at_ms=now,
            expires_at_ms=now + ttl_seconds * 1000,
            min_stop_bps=(
                Decimal(min_stop_bps) if min_stop_bps is not None else None
            ),
            max_stop_bps=(
                Decimal(max_stop_bps) if max_stop_bps is not None else None
            ),
        )

    def validate_freshness(self, now_ms: int) -> None:
        if now_ms < self.created_at_ms - 1000:
            raise RuleViolation("intent creation time is in the future")
        if now_ms > self.expires_at_ms:
            raise RuleViolation("entry intent has expired")

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "intent_id": self.intent_id,
            "source": self.source,
            "symbol": self.symbol,
            "side": self.side,
            "risk_usdt": str(self.risk_usdt),
            "stop_price": str(self.stop_price),
            "take_profit_price": (
                str(self.take_profit_price) if self.take_profit_price is not None else None
            ),
            "leverage": self.leverage,
            "margin_utilization": str(self.margin_utilization),
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "min_stop_bps": (
                str(self.min_stop_bps) if self.min_stop_bps is not None else None
            ),
            "max_stop_bps": (
                str(self.max_stop_bps) if self.max_stop_bps is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str | int | None]) -> "EntryIntent":
        return cls(
            intent_id=str(payload["intent_id"]),
            source=str(payload["source"]),
            symbol=str(payload["symbol"]),
            side=str(payload["side"]),
            risk_usdt=Decimal(str(payload["risk_usdt"])),
            stop_price=Decimal(str(payload["stop_price"])),
            take_profit_price=(
                Decimal(str(payload["take_profit_price"]))
                if payload.get("take_profit_price") is not None
                else None
            ),
            leverage=int(payload["leverage"]),
            margin_utilization=Decimal(str(payload["margin_utilization"])),
            created_at_ms=int(payload["created_at_ms"]),
            expires_at_ms=int(payload["expires_at_ms"]),
            min_stop_bps=(
                Decimal(str(payload["min_stop_bps"]))
                if payload.get("min_stop_bps") is not None
                else None
            ),
            max_stop_bps=(
                Decimal(str(payload["max_stop_bps"]))
                if payload.get("max_stop_bps") is not None
                else None
            ),
        )
