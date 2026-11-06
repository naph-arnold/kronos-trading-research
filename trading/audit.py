"""Structured JSON logging and hash-chained audit records.

Reuses the verified hash-chain implementation from research.six_agent_desk.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

from research.six_agent_desk import append_audit, verify_audit


def to_jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    return repr(value)


class AuditLog:
    """Append-only, single-writer, hash-chained JSONL file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: dict) -> str:
        return append_audit(self.path, to_jsonable(record))

    def verify(self) -> str:
        return verify_audit(self.path)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
                   "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update(to_jsonable(getattr(record, "fields", {})))
        return json.dumps(payload, sort_keys=True)


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={"fields": {"event": event, **fields}})
