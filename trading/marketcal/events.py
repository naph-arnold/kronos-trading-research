"""Economic-event schema and durable, revision-tracked event store."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from trading.common import ValidationError, check_identifier, require_utc

IMPACTS = ("low", "medium", "high")
STATUSES = ("scheduled", "tentative", "released", "cancelled", "postponed")


@dataclass(frozen=True)
class EconomicEvent:
    event_id: str
    name: str
    country: str
    currency: str
    scheduled_at: datetime  # UTC
    impact: str
    provider: str
    retrieved_at: datetime
    updated_at: datetime
    category: str = "macro"
    status: str = "scheduled"
    all_day: bool = False
    previous: str | None = None
    forecast: str | None = None
    actual: str | None = None
    source_url: str | None = None
    series: str | None = None  # canonical series key, e.g. US_CPI, US_NFP, US_GDP, FOMC_DECISION
    reference_period: str | None = None  # period the release describes, e.g. "2026-10" or "3rd Quarter 2026"

    def validate(self) -> None:
        check_identifier(self.event_id, "event_id")
        for name in ("scheduled_at", "retrieved_at", "updated_at"):
            require_utc(getattr(self, name), name)
        if self.impact not in IMPACTS or self.status not in STATUSES:
            raise ValidationError("Unknown impact or status")
        if not self.name.strip() or not self.provider.strip() or len(self.currency) != 3:
            raise ValidationError("Event requires name, provider and ISO currency")

    def to_json(self) -> dict:
        record = asdict(self)
        for name in ("scheduled_at", "retrieved_at", "updated_at"):
            record[name] = record[name].isoformat()
        return record

    @classmethod
    def from_json(cls, record: dict) -> "EconomicEvent":
        values = dict(record)
        for name in ("scheduled_at", "retrieved_at", "updated_at"):
            values[name] = datetime.fromisoformat(str(values[name]).replace("Z", "+00:00"))
        event = cls(**values)
        event.validate()
        return event


class EventStore:
    """SQLite store: latest version per event plus an append-only revision history,
    and per-provider refresh bookkeeping used for staleness checks."""

    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "scheduled_at TEXT NOT NULL, currency TEXT NOT NULL, impact TEXT NOT NULL, body TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS events_time ON events(scheduled_at);"
            "CREATE TABLE IF NOT EXISTS revisions (event_id TEXT NOT NULL, recorded_at TEXT NOT NULL, "
            "body TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS refreshes (provider TEXT PRIMARY KEY, refreshed_at TEXT NOT NULL, "
            "event_count INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS failures (provider TEXT PRIMARY KEY, failed_at TEXT NOT NULL, "
            "error TEXT NOT NULL);")

    def upsert(self, events: list[EconomicEvent]) -> dict:
        changed = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for event in events:
                    event.validate()
                    body = json.dumps(event.to_json(), sort_keys=True)
                    row = self._db.execute("SELECT body FROM events WHERE event_id=?", (event.event_id,)).fetchone()
                    if row and row[0] == body:
                        continue
                    if row and json.loads(row[0])["provider"] != event.provider:
                        raise ValidationError(f"Event {event.event_id} already owned by another provider")
                    self._db.execute(
                        "INSERT INTO events VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                        "scheduled_at=excluded.scheduled_at, currency=excluded.currency, impact=excluded.impact, "
                        "body=excluded.body",
                        (event.event_id, event.provider, event.scheduled_at.isoformat(timespec="seconds"),
                         event.currency, event.impact, body))
                    self._db.execute("INSERT INTO revisions VALUES (?,?,?)",
                                     (event.event_id, event.retrieved_at.isoformat(), body))
                    changed += 1
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {"received": len(events), "changed": changed}

    def record_refresh(self, provider: str, at: datetime, count: int) -> None:
        require_utc(at, "at")
        with self._lock:
            self._db.execute("INSERT INTO refreshes VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                             "refreshed_at=excluded.refreshed_at, event_count=excluded.event_count",
                             (provider, at.isoformat(), count))

    def record_failure(self, provider: str, at: datetime, error: str) -> None:
        require_utc(at, "at")
        with self._lock:
            self._db.execute("INSERT INTO failures VALUES (?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                             "failed_at=excluded.failed_at, error=excluded.error",
                             (provider, at.isoformat(), error[:500]))

    def last_failure(self, provider: str) -> tuple[datetime, str] | None:
        with self._lock:
            row = self._db.execute("SELECT failed_at, error FROM failures WHERE provider=?", (provider,)).fetchone()
        return (datetime.fromisoformat(row[0]), row[1]) if row else None

    def contradictions(self, start: datetime, end: datetime, *, tolerance: timedelta = timedelta(minutes=1),
                       window: timedelta = timedelta(days=3)) -> list[dict]:
        """Same `series` reported by different providers at times that disagree by more than `tolerance`
        (but within `window`, i.e. plausibly the same release). Such data is not trusted."""
        events = [e for e in self.between(start - window, end + window) if e.series and e.status != "cancelled"]
        conflicts = []
        for i, a in enumerate(events):
            for b in events[i + 1:]:
                if (a.series == b.series and a.provider != b.provider
                        and tolerance < abs(a.scheduled_at - b.scheduled_at) <= window):
                    conflicts.append({"series": a.series, "a": (a.provider, a.scheduled_at.isoformat()),
                                      "b": (b.provider, b.scheduled_at.isoformat())})
        return conflicts

    def last_refresh(self, provider: str) -> datetime | None:
        with self._lock:
            row = self._db.execute("SELECT refreshed_at FROM refreshes WHERE provider=?", (provider,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def between(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        require_utc(start, "start")
        require_utc(end, "end")
        with self._lock:
            rows = self._db.execute("SELECT body FROM events WHERE scheduled_at >= ? AND scheduled_at <= ? "
                                    "ORDER BY scheduled_at", (start.isoformat(timespec="seconds"),
                                                              end.isoformat(timespec="seconds"))).fetchall()
        return [EconomicEvent.from_json(json.loads(r[0])) for r in rows]

    def revisions(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT recorded_at, body FROM revisions WHERE event_id=? ORDER BY rowid",
                                    (event_id,)).fetchall()
        return [{"recorded_at": r[0], **json.loads(r[1])} for r in rows]


def load_events_file(path: Path, *, provider: str) -> list[EconomicEvent]:
    """Import a structured export from a licensed provider (JSON list of EconomicEvent fields).

    The file's provider field must match `provider`; mixed or unattributed data is rejected.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValidationError("Event file must be a JSON list")
    events = []
    for record in raw:
        try:
            event = EconomicEvent.from_json(record)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValidationError(f"Malformed event record: {str(record)[:120]}") from exc
        if event.provider != provider:
            raise ValidationError(f"Event provider {event.provider!r} does not match {provider!r}")
        events.append(event)
    return events


