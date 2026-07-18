from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .journal import OrderJournal


SENSITIVE_KEYS = {"signature", "api_key", "api_secret", "x-mbx-apikey", "secret"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if context:
            payload["context"] = redact(context)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("autotrade")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = JsonFormatter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


class AlertManager:
    def __init__(self, journal: OrderJournal, logger: logging.Logger) -> None:
        self.journal = journal
        self.logger = logger

    def emit(
        self,
        event_type: str,
        message: str,
        *,
        symbol: str | None = None,
        severity: str = "ERROR",
        payload: dict[str, Any] | None = None,
    ) -> None:
        context = {"event_type": event_type, "symbol": symbol, **(payload or {})}
        level = getattr(logging, severity.upper(), logging.ERROR)
        self.logger.log(level, message, extra={"context": context})
        self.journal.append_audit(
            "alert",
            event_type,
            symbol=symbol,
            severity=severity.upper(),
            payload={"message": message, **(payload or {})},
        )
