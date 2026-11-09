"""Phase 4 preparation: six Grok agents in ADVISORY-ONLY mode (official xAI Grok API; NOT activated).

What it does: for a shadow signal (or a market brief) it builds a read-only context from a shadow store,
asks the five specialists (Scout, News, Chart, Kronos Analyst, Skeptic) and then the Coordinator, validates
every output against `trading.agents.desk` / `trading.agents.news`, and records requests, raw responses,
validation results and the Coordinator summary in its OWN store (state/grok_advisory/advisory.sqlite).

What it cannot do: it opens shadow stores read-only (SQLite `mode=ro`), never writes to them, never calls a
venue, and nothing it produces is passed to a strategy, a shadow engine or `trading.risk.evaluate`. Frozen
strategy decisions are unchanged whatever the agents say.

Activation (all required; none is set in this milestone):
  1. research/configs/grok_advisory.json: "activated": true and an operator-chosen "model"
  2. environment: GROK_AGENTS_ACTIVATED=1 and XAI_API_KEY (never committed, never logged)
  3. command line: --transport xai --i-have-activation-approval
Without them the `xai` transport raises GrokNotActivated. `dry-run` makes no calls (every role is recorded
as unavailable -> Coordinator fallback `insufficient_inputs`); `replay` uses recorded JSON responses.

Usage: python -m research.grok_advisory review --db <shadow.sqlite> --runner xau_orb [--signal-id ID | --latest N]
       python -m research.grok_advisory review-signals [--runner R]   (new candidate signals only; the trigger)
       python -m research.grok_advisory verify-audit
       python -m research.grok_advisory status
       python -m research.grok_advisory estimate-cost [--runner xau_orb --db <shadow.sqlite>]   (offline)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading.agents.desk import (COORDINATOR_RECOMMENDATIONS, PROVIDER, RECOMMENDATIONS, ROLE_BRIEFS, SPECIALISTS,
                                 AdvisoryContext, RoleOutput, deterministic_coordinator_fallback, output_schema,
                                 parse_role_output)
from trading.agents.mtf_advisory import OFFICIAL_XAI_BASE_URL, GrokNotActivated
from trading.agents.news import ALLOWED_KEYS as NEWS_KEYS
from trading.agents.news import RECOMMENDATIONS as NEWS_RECOMMENDATIONS
from trading.agents.news import parse_news_assessment
from trading.common import ValidationError

DEFAULT_CONFIG = "research/configs/grok_advisory.json"
HARD_DAILY_USD_CEILING = 1.00  # operator instruction 2026-11-09: hard API spending ceiling of $1 per day
CHAT_PATH = "/v1/chat/completions"  # OpenAI-compatible endpoint of the official xAI API (verify at activation)


# --------------------------------------------------------------------------- transports

class DryRunTransport:
    name = "dry-run"

    def complete(self, role: str, system: str, user: str) -> str:
        raise GrokNotActivated("dry-run: no AI call made")


class ReplayTransport:
    """Recorded responses: {role: payload | {subject_id: payload}}. For tests and offline rehearsal."""

    name = "replay"

    def __init__(self, responses: dict):
        self.responses = responses

    def complete(self, role: str, system: str, user: str) -> str:
        entry = self.responses.get(role)
        if entry is None:
            raise GrokNotActivated(f"replay: no recorded response for {role}")
        subject = json.loads(user)["subject"]["id"]
        if isinstance(entry, dict) and subject in entry:
            entry = entry[subject]
        return entry if isinstance(entry, str) else json.dumps(entry)


class XaiTransport:
    """Official xAI Grok API only. The key is read from the environment at call time and never stored, logged or
    included in errors."""

    name = "xai"

    def __init__(self, *, model: str, base_url: str = OFFICIAL_XAI_BASE_URL, key_env: str = "XAI_API_KEY",
                 timeout_s: float = 30.0, max_tokens: int = 800, opener=urllib.request.urlopen):
        if base_url != OFFICIAL_XAI_BASE_URL:
            raise ValueError("Reasoning agents must use the official xAI Grok API (https://api.x.ai)")
        if not model or not isinstance(model, str):
            raise ValueError("An operator-chosen xAI model id is required")
        self.model, self.base_url, self.key_env = model, base_url, key_env
        self.timeout_s, self.max_tokens, self._open = timeout_s, max_tokens, opener
        self.last_usage: dict | None = None

    def complete(self, role: str, system: str, user: str) -> str:
        key = os.environ.get(self.key_env, "")
        if not key:
            raise GrokNotActivated(f"{self.key_env} is not set")
        body = {"model": self.model, "temperature": 0, "max_tokens": self.max_tokens,
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": f"kronos_advisory_{role}", "schema": role_json_schema(role), "strict": True}},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(self.base_url + CHAT_PATH, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise GrokNotActivated(f"xAI request failed: {type(exc).__name__}: {str(exc).replace(key, '***')}") from None
        usage = data.get("usage") if isinstance(data, dict) else None
        self.last_usage = ({k: v for k, v in usage.items() if isinstance(v, (int, float))}
                           if isinstance(usage, dict) else None)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError):
            raise GrokNotActivated("xAI response missing choices[0].message.content") from None
        # Incomplete answers are failures, never partial advice: truncated (max_tokens), refused or empty.
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        if finish not in (None, "stop"):
            raise GrokNotActivated(f"xAI response incomplete (finish_reason={finish!r})")
        if isinstance(message, dict) and message.get("refusal"):
            raise GrokNotActivated("xAI response was a refusal")
        if not isinstance(content, str) or not content.strip():
            raise GrokNotActivated("xAI response content is empty")
        return content


_TEXTS = {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 500}}
_CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
_MINUTES = {"type": "integer", "minimum": 1, "maximum": 240}


def role_json_schema(role: str) -> dict:
    """Strict JSON Schema sent as xAI `response_format` (structured outputs). It mirrors the local validators in
    `trading.agents.desk` / `trading.agents.news`, which remain authoritative: every response is validated again
    locally (evidence ids, timestamps, price range, forbidden keys), whatever the API guarantees."""
    if role == "news":
        props = {
            "schema_version": {"type": "integer", "enum": [1]}, "role": {"type": "string", "enum": ["news"]},
            "provider": {"type": "string", "enum": [PROVIDER]}, "model": {"type": "string", "maxLength": 100},
            "assessed_at": {"type": "string", "format": "date-time"},
            "markets": {"type": "array", "minItems": 1, "maxItems": 2,
                        "items": {"type": "string", "enum": ["xauusd_mt5", "btcusdt_binance_spot"]}},
            "event_ids": _TEXTS,
            "headlines": {"type": "array", "maxItems": 20, "items": {
                "type": "object", "additionalProperties": False, "required": ["title", "url", "published_at"],
                "properties": {"title": {"type": "string", "maxLength": 500}, "url": {"type": "string"},
                               "published_at": {"type": "string"}}}},
            "interpretation": {"type": "string", "minLength": 1, "maxLength": 4000},
            "implications": {"type": "object", "additionalProperties": False, "properties": {
                "xauusd_mt5": {"type": "string", "maxLength": 1000},
                "btcusdt_binance_spot": {"type": "string", "maxLength": 1000}}},
            "geopolitical_risks": _TEXTS, "abnormal_conditions": _TEXTS, "conflicts": _TEXTS,
            "confidence": _CONFIDENCE, "recommendation": {"type": "string", "enum": list(NEWS_RECOMMENDATIONS)},
            "valid_for_minutes": _MINUTES}
        return {"type": "object", "additionalProperties": False, "properties": props, "required": sorted(props)}
    recs = COORDINATOR_RECOMMENDATIONS if role == "coordinator" else RECOMMENDATIONS
    props = {
        "schema_version": {"type": "integer", "enum": [1]}, "role": {"type": "string", "enum": [role]},
        "provider": {"type": "string", "enum": [PROVIDER]}, "model": {"type": "string", "maxLength": 100},
        "assessed_at": {"type": "string", "format": "date-time"},
        "subject": {"type": "object", "additionalProperties": False, "required": ["kind", "id"],
                    "properties": {"kind": {"type": "string"}, "id": {"type": "string"}}},
        "assessment": {"type": "string", "minLength": 1, "maxLength": 2000},
        "evidence": _TEXTS, "concerns": _TEXTS, "confidence": _CONFIDENCE,
        "recommendation": {"type": "string", "enum": list(recs)}, "valid_for_minutes": _MINUTES}
    required = sorted(props)
    if role == "chart":
        props["levels"] = {"type": "array", "maxItems": 10, "items": {"type": "number"}}  # optional
    if role == "kronos_analyst":
        props["kronos_forecast_id"] = {"type": ["string", "null"]}
        required.append("kronos_forecast_id")
    if role == "coordinator":
        props["role_inputs"] = {"type": "array", "maxItems": 5, "items": {"type": "string", "enum": list(SPECIALISTS)}}
        props["disagreements"] = _TEXTS
        required += ["role_inputs", "disagreements"]
    return {"type": "object", "additionalProperties": False, "properties": props, "required": sorted(required)}


def make_transport(config: dict, kind: str, *, approved: bool, env=os.environ, replay: dict | None = None):
    if kind == "dry-run":
        return DryRunTransport()
    if kind == "replay":
        return ReplayTransport(replay or {})
    if kind != "xai":
        raise ValueError(f"Unknown transport {kind!r}")
    if not (config.get("activated") is True and env.get("GROK_AGENTS_ACTIVATED") == "1" and approved):
        raise GrokNotActivated("Grok agents are not activated: config 'activated', GROK_AGENTS_ACTIVATED=1 and "
                               "--i-have-activation-approval are all required")
    pricing = config.get("pricing_usd_per_million") or {}
    if not all(isinstance(pricing.get(k), (int, float)) and pricing[k] > 0 for k in ("input", "output")) or not (
            isinstance(config.get("daily_usd_budget"), (int, float)) and config["daily_usd_budget"] > 0):
        raise GrokNotActivated("xAI pricing (input/output USD per million tokens, from docs.x.ai) and a positive "
                               "daily_usd_budget must be set before activation")
    if config["daily_usd_budget"] > HARD_DAILY_USD_CEILING:
        raise GrokNotActivated(f"daily_usd_budget exceeds the operator-approved hard ceiling of "
                               f"${HARD_DAILY_USD_CEILING:.2f}/day (raising it needs a reviewed code change)")
    return XaiTransport(model=config.get("model") or "", base_url=config["base_url"],
                        timeout_s=config["timeout_s"], max_tokens=config["max_tokens"])


# --------------------------------------------------------------------------- independent audit log

class AuditLog:
    """Append-only JSONL audit trail, independent of the SQLite store: one line per call, validated output and
    summary, each carrying the SHA-256 of the previous line (hash chain), so deletions, edits or reordering are
    detectable with `verify-audit`. It records hashes, status, costs and token usage - never prompts, raw responses
    or credentials (the key never reaches this module's records)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev, self._seq = "0" * 64, 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._prev, self._seq = rec["sha256"], rec["seq"]

    @staticmethod
    def _digest(record: dict) -> str:
        body = {k: v for k, v in record.items() if k != "sha256"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def append(self, kind: str, **fields) -> None:
        record = {"seq": self._seq + 1, "kind": kind, "logged_at": datetime.now(timezone.utc).isoformat(),
                  "prev_sha256": self._prev, **fields}
        record["sha256"] = self._digest(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._prev, self._seq = record["sha256"], record["seq"]

    @classmethod
    def verify(cls, path: Path) -> dict:
        prev, seq, counts, problems = "0" * 64, 0, {}, []
        lines = Path(path).read_text(encoding="utf-8").splitlines() if Path(path).exists() else []
        for n, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {n}: not JSON")
                break
            if rec.get("seq") != seq + 1 or rec.get("prev_sha256") != prev or rec.get("sha256") != cls._digest(rec):
                problems.append(f"line {n}: chain broken (seq {rec.get('seq')})")
                break
            prev, seq = rec["sha256"], rec["seq"]
            counts[rec["kind"]] = counts.get(rec["kind"], 0) + 1
        return {"ok": not problems, "records": seq, "by_kind": counts, "problems": problems}


# --------------------------------------------------------------------------- advisory store (separate)

class AdvisoryStore:
    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, role TEXT NOT NULL,"
        " subject TEXT NOT NULL, transport TEXT NOT NULL, model TEXT, context_sha256 TEXT NOT NULL,"
        " prompt_sha256 TEXT NOT NULL, response_sha256 TEXT, status TEXT NOT NULL, error TEXT,"
        " est_cost_usd REAL, usage TEXT, actual_cost_usd REAL)",
        "CREATE TABLE IF NOT EXISTS outputs (subject TEXT NOT NULL, role TEXT NOT NULL, at TEXT NOT NULL,"
        " valid INTEGER NOT NULL, payload TEXT, error TEXT, PRIMARY KEY (subject, role, at))",
        "CREATE TABLE IF NOT EXISTS summaries (subject TEXT NOT NULL, at TEXT NOT NULL, runner TEXT NOT NULL,"
        " context_sha256 TEXT NOT NULL, recommendation TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (subject, at))",
    )

    def __init__(self, path: Path, audit: AuditLog | None = None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit if audit is not None else AuditLog(Path(path).parent / "audit.jsonl")
        self.db = sqlite3.connect(str(path))
        for stmt in self.SCHEMA:
            self.db.execute(stmt)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
        for column, kind in (("est_cost_usd", "REAL"), ("usage", "TEXT"), ("actual_cost_usd", "REAL")):  # stores created before cost tracking
            if column not in columns:
                self.db.execute(f"ALTER TABLE calls ADD COLUMN {column} {kind}")
        self.db.commit()

    def calls_since(self, since: datetime, transport: str = "xai") -> int:
        return self.db.execute("SELECT COUNT(*) FROM calls WHERE at >= ? AND transport = ?",
                               (since.isoformat(), transport)).fetchone()[0]

    def est_cost_since(self, since: datetime, transport: str = "xai") -> float:
        return self.db.execute("SELECT COALESCE(SUM(est_cost_usd), 0) FROM calls WHERE at >= ? AND transport = ?",
                               (since.isoformat(), transport)).fetchone()[0]

    def call(self, **row) -> None:
        row = {"est_cost_usd": None, "usage": None, "actual_cost_usd": None, **row}
        if isinstance(row["usage"], dict):
            row["usage"] = json.dumps(row["usage"], sort_keys=True)
        self.db.execute("INSERT INTO calls (at, role, subject, transport, model, context_sha256, prompt_sha256,"
                        " response_sha256, status, error, est_cost_usd, usage, actual_cost_usd) VALUES (:at, :role, :subject,"
                        " :transport, :model, :context_sha256, :prompt_sha256, :response_sha256, :status, :error,"
                        " :est_cost_usd, :usage, :actual_cost_usd)", row)
        self.db.commit()
        self.audit.append("call", **{k: row[k] for k in (
            "at", "role", "subject", "transport", "model", "context_sha256", "prompt_sha256", "response_sha256",
            "status", "error", "est_cost_usd", "actual_cost_usd", "usage")})

    def output(self, subject: str, role: str, at: str, valid: bool, payload, error: str | None) -> None:
        text = None if payload is None else json.dumps(payload, default=str)
        self.db.execute("INSERT OR REPLACE INTO outputs VALUES (?, ?, ?, ?, ?, ?)",
                        (subject, role, at, int(valid), text, error))
        self.db.commit()
        self.audit.append("output", at=at, subject=subject, role=role, valid=bool(valid), error=error,
                          payload_sha256=None if text is None else hashlib.sha256(text.encode()).hexdigest())

    def summary(self, subject: str, at: str, runner: str, context_sha: str, body: dict) -> None:
        text = json.dumps(body, default=str)
        self.db.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?, ?)",
                        (subject, at, runner, context_sha, body["recommendation"], text))
        self.db.commit()
        self.audit.append("summary", at=at, subject=subject, runner=runner, context_sha256=context_sha,
                          recommendation=body["recommendation"], source=body.get("source"),
                          skipped=body.get("skipped"), errors=sorted(body.get("errors", {})),
                          body_sha256=hashlib.sha256(text.encode()).hexdigest())

    def reviewed(self, subject: str, transport: str) -> bool:
        """A subject counts as reviewed once any call was recorded for it with this transport."""
        return self.db.execute("SELECT 1 FROM calls WHERE subject = ? AND transport = ? LIMIT 1",
                               (subject, transport)).fetchone() is not None

    def counts(self) -> dict:
        q = self.db.execute
        return {"calls": q("SELECT COUNT(*) FROM calls").fetchone()[0],
                "valid_outputs": q("SELECT COUNT(*) FROM outputs WHERE valid = 1").fetchone()[0],
                "invalid_outputs": q("SELECT COUNT(*) FROM outputs WHERE valid = 0").fetchone()[0],
                "summaries": dict(q("SELECT recommendation, COUNT(*) FROM summaries GROUP BY recommendation"))}


# --------------------------------------------------------------------------- read-only context

def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def recent_signals(db: Path, *, limit: int = 5, signal_id: str | None = None) -> list[dict]:
    con = _ro(db)
    try:
        if signal_id:
            rows = con.execute("SELECT strategy_key, signal_id, bar_close, status, body FROM signals "
                               "WHERE signal_id = ?", (signal_id,)).fetchall()
        else:
            rows = con.execute("SELECT strategy_key, signal_id, bar_close, status, body FROM signals "
                               "WHERE status != 'warmup' ORDER BY bar_close DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [{"strategy_key": r[0], "signal_id": r[1], "bar_close": r[2], "status": r[3], "body": json.loads(r[4])}
            for r in rows]


_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?%?")


def sanitize_reason(reason) -> str:
    """Risk-engine reasons can quote limits and sizes ("Spread 0.6 exceeds max 0.5", "Margin ... exceeds 0.3 of
    free margin"). The agents get the reason category only: every number is masked, so no risk limit, size,
    equity or strategy parameter reaches a prompt."""
    return _NUMBER.sub("#", str(reason))[:200]


# Signal statuses written by trading.shadow.engine: warmup (before the forward boundary), rejected_unanchored,
# pending (awaiting the deterministic decision), evaluated (decided by the risk engine), expired (latency guard).
REVIEWABLE_STATUSES = ("evaluated",)


def candidate_signals(db: Path, *, strategies: list[str], since: datetime) -> list[dict]:
    """Forward signals of the runner's CANDIDATE strategies (never random controls or warm-up), already decided by
    the deterministic risk engine, with bar_close >= since. Read-only."""
    if not strategies:
        return []
    con = _ro(db)
    try:
        smarks, kmarks = ",".join("?" * len(REVIEWABLE_STATUSES)), ",".join("?" * len(strategies))
        rows = con.execute(f"SELECT strategy_key, signal_id, bar_close, status, body FROM signals WHERE status IN "
                           f"({smarks}) AND strategy_key IN ({kmarks}) ORDER BY bar_close",
                           (*REVIEWABLE_STATUSES, *strategies)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        close = datetime.fromisoformat(str(r[2]).replace("Z", "+00:00"))
        if close >= since:
            out.append({"strategy_key": r[0], "signal_id": r[1], "bar_close": r[2], "status": r[3],
                        "body": json.loads(r[4])})
    return out


def signal_context(db: Path, signal: dict, *, runner: str, market: str, now: datetime, bars: int = 96,
                   kronos: dict | None = None) -> AdvisoryContext:
    """Facts available at the signal's bar close only (no later bars), plus the frozen decisions taken on it."""
    con = _ro(db)
    try:
        rows = con.execute("SELECT open_time, open, high, low, close, interval FROM bars WHERE open_time < ? "
                           "ORDER BY open_time DESC LIMIT ?", (signal["bar_close"], bars)).fetchall()[::-1]
        decisions = con.execute("SELECT book_id, approved, reasons FROM decisions WHERE signal_id = ?",
                                (signal["signal_id"],)).fetchall()
    finally:
        con.close()
    bar_facts = [{"id": f"bar:{r[0]}", "open_time": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                  "c": float(r[4]), "interval": r[5]} for r in rows]
    decision_facts = [{"id": f"decision:{d[0]}:{signal['signal_id']}", "book": d[0], "approved": bool(d[1]),
                       "reasons": [sanitize_reason(r) for r in json.loads(d[2])]} for d in decisions]
    body = signal["body"]
    sig_fact = {"id": f"signal:{signal['signal_id']}", "strategy_key": signal["strategy_key"], "side": body.get("side"),
                "bar_close": signal["bar_close"], "status": signal["status"],
                "note": "Frozen strategy signal; its decisions were taken by the deterministic risk engine."}
    kronos = kronos or {}
    ids = {sig_fact["id"]} | {b["id"] for b in bar_facts} | {d["id"] for d in decision_facts}
    prices = [b["h"] for b in bar_facts] + [b["l"] for b in bar_facts]
    return AdvisoryContext(
        subject_kind="signal", subject_id=f"{runner}:{signal['signal_id']}", market=market,
        as_of=datetime.fromisoformat(signal["bar_close"]), evidence_ids=frozenset(ids),
        price_range=(min(prices), max(prices)) if prices else None, kronos_forecast_ids=frozenset(kronos),
        facts={"signal": sig_fact, "bars": bar_facts, "decisions": decision_facts,
               "kronos_forecasts": [{"id": k, **v} for k, v in sorted(kronos.items())], "advisory_generated_at":
               now.isoformat()})


def calendar_snapshot(calendar_db: Path, workdir: Path):
    """Copy a runner's calendar.sqlite through a read-only connection, so the News role can be validated against
    the verified events without ever opening the runner's file for writing."""
    from trading.marketcal.events import EventStore
    dest = Path(workdir) / "calendar_snapshot.sqlite"
    src = _ro(calendar_db)
    try:
        out = sqlite3.connect(str(dest))
        src.backup(out)
        out.close()
    finally:
        src.close()
    return EventStore(dest)


# --------------------------------------------------------------------------- cost control

CHARS_PER_TOKEN = 3.0  # conservative for JSON-heavy prompts (numbers and punctuation tokenize densely)
CALLS_PER_REVIEW = len(SPECIALISTS) + 1


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def call_cost_usd(input_tokens: int, output_tokens: int, pricing: dict) -> float:
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def worst_case_call_cost(system: str, user: str, max_tokens: int, pricing: dict) -> float:
    """Upper bound used by the daily USD guard: estimated prompt tokens plus the full max_tokens of output."""
    return call_cost_usd(estimate_tokens(system + user), max_tokens, pricing)


# --------------------------------------------------------------------------- desk

def _prompt(role: str, context: AdvisoryContext, extra: dict | None = None) -> tuple[str, str]:
    if role == "news":
        shape = {k: "<see contract>" for k in sorted(NEWS_KEYS)}
        shape.update(schema_version=1, role="news", provider=PROVIDER,
                     recommendation=["no_view", "caution", "avoid_new_entries"])
    else:
        shape = output_schema(role)
    system = (f"You are the {role} agent of an ADVISORY-ONLY trading research desk. {ROLE_BRIEFS[role]} "
              "You cannot place, approve, size, block or modify trades, stops, targets, risk limits, parameters or "
              "schedules; a deterministic risk engine has already decided. Cite only evidence ids present in the "
              "user message. Return ONLY a JSON object with exactly this shape: " + json.dumps(shape))
    user = json.dumps({"subject": {"kind": context.subject_kind, "id": context.subject_id}, "market": context.market,
                       "as_of": context.as_of.isoformat(), "facts": context.facts, **(extra or {})}, default=str)
    return system, user


def _news_as_role_output(n) -> RoleOutput:
    return RoleOutput(role="news", model=n.model, assessed_at=n.assessed_at, subject={}, assessment=n.interpretation,
                      evidence=n.event_ids, concerns=n.conflicts, confidence=n.confidence,
                      recommendation=n.recommendation, valid_for_minutes=n.valid_for_minutes, extra={})


def run_desk(context: AdvisoryContext, transport, store: AdvisoryStore, *, runner: str, now: datetime,
             model: str | None, event_store=None, daily_call_budget: int = 0, daily_usd_budget: float = 0.0,
             pricing: dict | None = None) -> dict:
    """Ask the five specialists, then the Coordinator. Every output is validated; failures are recorded and the
    Coordinator falls back to `insufficient_inputs`. Returns the recorded summary (advisory only)."""
    context_sha = context.digest()
    at = now.isoformat()
    daily_usd_budget = min(daily_usd_budget, HARD_DAILY_USD_CEILING)
    valid: dict[str, RoleOutput] = {}
    errors: dict[str, str] = {}
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    paid = transport.name == "xai"
    if paid and store.calls_since(day) + CALLS_PER_REVIEW > daily_call_budget:
        # all-or-nothing: never start a review the call budget cannot finish
        errors = {r: "daily call budget cannot cover a full review; not started" for r in SPECIALISTS}
        summary = deterministic_coordinator_fallback(context, {}, errors, now=now)
        summary.update(specialists={}, errors=errors, skipped="budget")
        store.summary(context.subject_id, at, runner, context_sha, summary)
        return summary

    def ask(role: str, extra: dict | None = None):
        system, user = _prompt(role, context, extra)
        row = {"at": at, "role": role, "subject": context.subject_id, "transport": transport.name, "model": model,
               "context_sha256": context_sha, "prompt_sha256": hashlib.sha256((system + user).encode()).hexdigest(),
               "response_sha256": None, "status": "ok", "error": None}
        if paid:
            if pricing is None:
                row.update(status="skipped", error="no pricing configured (fail closed)")
                store.call(**row)
                return None, "no pricing configured"
            worst = worst_case_call_cost(system, user, transport.max_tokens, pricing)
            if store.calls_since(day) >= daily_call_budget:
                row.update(status="skipped", error="daily call budget reached")
                store.call(**row)
                return None, "daily call budget reached"
            if store.est_cost_since(day) + worst > daily_usd_budget:
                row.update(status="skipped", error="daily USD budget reached")
                store.call(**row)
                return None, "daily USD budget reached"
            row["est_cost_usd"] = worst
        try:
            text = transport.complete(role, system, user)
        except GrokNotActivated as exc:
            row.update(status="unavailable", error=str(exc)[:300])
            store.call(**row)
            return None, f"unavailable: {exc}"[:300]
        row["response_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        usage = getattr(transport, "last_usage", None)
        if paid and usage and pricing is not None:
            row["usage"] = usage
            # actual = billed tokens at list price (cached-input discounts ignored, so never under-counted);
            # the guard keeps the larger of the worst-case reservation and the actual cost
            row["actual_cost_usd"] = call_cost_usd(int(usage.get("prompt_tokens", 0)),
                                                   int(usage.get("completion_tokens", 0)), pricing)
            row["est_cost_usd"] = max(row["est_cost_usd"] or 0.0, row["actual_cost_usd"])
        store.call(**row)
        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            return None, "response is not JSON"

    for role in SPECIALISTS:
        payload, err = ask(role)
        if payload is not None:
            try:
                if role == "news":
                    if event_store is None:
                        raise ValidationError("no verified calendar snapshot supplied for the News role")
                    valid[role] = _news_as_role_output(parse_news_assessment(payload, store=event_store, now=now))
                else:
                    valid[role] = parse_role_output(payload, role=role, context=context, now=now)
            except (ValidationError, KeyError, TypeError, ValueError) as exc:
                err = f"invalid: {exc}"[:300]
        if err:
            errors[role] = err
        store.output(context.subject_id, role, at, role in valid, payload, err)
    summary = None
    if len(valid) == len(SPECIALISTS):
        inputs = {r: {k: v for k, v in asdict(o).items() if k not in ("assessed_at",)} for r, o in valid.items()}
        payload, err = ask("coordinator", {"validated_specialist_outputs": inputs})
        if payload is not None:
            try:
                out = parse_role_output(payload, role="coordinator", context=context, now=now, validated_inputs=valid)
                summary = {"role": "coordinator", "source": "grok", "model": out.model, "assessed_at": at,
                           "subject": out.subject, "recommendation": out.recommendation, "assessment": out.assessment,
                           "disagreements": out.extra["disagreements"], "valid_inputs": sorted(valid),
                           "advisory_only": True}
            except ValidationError as exc:
                err = f"invalid: {exc}"[:300]
        store.output(context.subject_id, "coordinator", at, summary is not None, payload, err)
        if summary is None:
            errors["coordinator"] = err or "missing"
    if summary is None:
        summary = deterministic_coordinator_fallback(context, valid, errors, now=now)
    summary["specialists"] = {r: {"recommendation": o.recommendation, "confidence": o.confidence}
                              for r, o in valid.items()}
    summary["errors"] = errors
    store.summary(context.subject_id, at, runner, context_sha, summary)
    return summary


# --------------------------------------------------------------------------- CLI

def cmd_review(args) -> dict:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    replay = json.loads(Path(args.replay_file).read_text(encoding="utf-8")) if args.replay_file else None
    transport = make_transport(config, args.transport, approved=args.i_have_activation_approval, replay=replay)
    store = AdvisoryStore(Path(args.advisory_dir or config["advisory_dir"]) / "advisory.sqlite")
    now = datetime.now(timezone.utc)
    kronos = json.loads(Path(args.kronos_file).read_text(encoding="utf-8")) if args.kronos_file else None
    runner = config["runners"][args.runner]
    db = Path(args.db or runner["shadow_db"])
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        cal = runner.get("calendar_db")
        events = calendar_snapshot(Path(cal), Path(tmp)) if cal and Path(cal).exists() else None
        for sig in recent_signals(db, limit=args.latest, signal_id=args.signal_id):
            ctx = signal_context(db, sig, runner=args.runner, market=runner["market"], now=now, kronos=kronos)
            out.append(run_desk(ctx, transport, store, runner=args.runner, now=now, model=config.get("model"),
                                event_store=events, daily_call_budget=config["daily_call_budget"],
                                daily_usd_budget=float(config.get("daily_usd_budget") or 0),
                                pricing=config.get("pricing_usd_per_million")))
        if events is not None:
            events._db.close()
    return {"transport": transport.name, "reviewed": len(out),
            "recommendations": [s["recommendation"] for s in out], "store": store.counts()}


def review_new_signals(config: dict, transport, store: AdvisoryStore, *, now: datetime, runners: list[str],
                       kronos: dict | None = None, db_overrides: dict | None = None) -> dict:
    """Signal-triggered reviews (no continuous polling of the API): reads each runner's shadow store read-only,
    selects forward signals of its candidate strategies that the risk engine has already decided, skips those
    already reviewed with this transport or older than `max_signal_age_hours`, and reviews at most
    `max_reviews_per_run`. With no new signal, no AI call is made."""
    trigger = config.get("review_trigger") or {}
    max_age = float(trigger.get("max_signal_age_hours", 6))
    budget_left = int(trigger.get("max_reviews_per_run", 3))
    since = now - timedelta(hours=max_age)
    report = {"transport": transport.name, "runners": {}, "reviewed": 0, "recommendations": []}
    for name in runners:
        runner = config["runners"][name]
        db = Path((db_overrides or {}).get(name) or runner["shadow_db"])
        entry = {"candidates": runner.get("candidate_strategies", []), "new": 0, "already_reviewed": 0,
                 "deferred": 0}
        if not db.exists():
            entry["status"] = "no shadow store (runner not started)"
            report["runners"][name] = entry
            continue
        todo = []
        for sig in candidate_signals(db, strategies=entry["candidates"], since=since):
            subject = f"{name}:{sig['signal_id']}"
            if store.reviewed(subject, transport.name):
                entry["already_reviewed"] += 1
            else:
                todo.append(sig)
        entry["new"] = len(todo)
        with tempfile.TemporaryDirectory() as tmp:
            cal = runner.get("calendar_db")
            events = calendar_snapshot(Path(cal), Path(tmp)) if todo and cal and Path(cal).exists() else None
            try:
                for sig in todo:
                    if budget_left <= 0:
                        entry["deferred"] += 1
                        continue
                    budget_left -= 1
                    ctx = signal_context(db, sig, runner=name, market=runner["market"], now=now, kronos=kronos)
                    summary = run_desk(ctx, transport, store, runner=name, now=now, model=config.get("model"),
                                       event_store=events, daily_call_budget=config["daily_call_budget"],
                                       daily_usd_budget=float(config.get("daily_usd_budget") or 0),
                                       pricing=config.get("pricing_usd_per_million"))
                    report["reviewed"] += 1
                    report["recommendations"].append({"subject": ctx.subject_id,
                                                      "recommendation": summary["recommendation"]})
            finally:
                if events is not None:
                    events._db.close()
        report["runners"][name] = entry
    return report


def cmd_review_signals(args) -> dict:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    transport = make_transport(config, args.transport, approved=args.i_have_activation_approval)
    store = AdvisoryStore(Path(args.advisory_dir or config["advisory_dir"]) / "advisory.sqlite")
    kronos = json.loads(Path(args.kronos_file).read_text(encoding="utf-8")) if args.kronos_file else None
    runners = [args.runner] if args.runner else sorted(config["runners"])
    overrides = dict(item.split("=", 1) for item in (args.db or []))
    unknown = set(overrides) - set(config["runners"])
    if unknown:
        raise SystemExit(f"--db names unknown runners: {sorted(unknown)}")
    out = review_new_signals(config, transport, store, now=datetime.now(timezone.utc), runners=runners,
                             kronos=kronos, db_overrides=overrides)
    out["store"] = store.counts()
    return out


def cmd_verify_audit(args) -> dict:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base = Path(args.advisory_dir or config["advisory_dir"])
    result = AuditLog.verify(base / "audit.jsonl")
    db = base / "advisory.sqlite"
    if db.exists():  # cross-check: every SQLite call row must have its audit line
        con = _ro(db)
        try:
            calls = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        finally:
            con.close()
        result["sqlite_calls"] = calls
        if result["by_kind"].get("call", 0) != calls:
            result["ok"] = False
            result["problems"].append(f"audit has {result['by_kind'].get('call', 0)} call records, store has {calls}")
    return result


def representative_context(db: Path | None, *, runner: str, market: str, now: datetime, bars: int = 96):
    """A full-size context for cost estimation: the latest `bars` bars of a shadow store (read-only) and a
    placeholder signal with one decision per book. No AI call; nothing is recorded."""
    rows = []
    if db is not None and Path(db).exists():
        con = _ro(Path(db))
        try:
            rows = con.execute("SELECT open_time, open, high, low, close, interval FROM bars ORDER BY open_time DESC "
                               "LIMIT ?", (bars,)).fetchall()[::-1]
            books = [r[0] for r in con.execute("SELECT book_id FROM books ORDER BY book_id")]
        finally:
            con.close()
    else:
        books = [f"book{i}" for i in range(24)]
    if not rows:
        rows = [(f"2026-01-01T00:{i:02d}:00+00:00", 4000.0, 4001.5, 3998.5, 4000.25, "5m") for i in range(bars)]
    close = rows[-1][0]
    bar_facts = [{"id": f"bar:{r[0]}", "open_time": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                  "c": float(r[4]), "interval": r[5]} for r in rows]
    decisions = [{"id": f"decision:{b}:estimate", "book": b, "approved": False,
                  "reasons": ["Planned risk exceeds the per-trade limit (example reason text of typical length)"]}
                 for b in books]
    sig = {"id": "signal:estimate", "strategy_key": "estimate", "side": "long", "bar_close": close,
           "status": "forward", "note": "Frozen strategy signal; its decisions were taken by the deterministic "
                                        "risk engine."}
    prices = [b["h"] for b in bar_facts] + [b["l"] for b in bar_facts]
    return AdvisoryContext(subject_kind="signal", subject_id=f"{runner}:estimate", market=market, as_of=now,
                           evidence_ids=frozenset([sig["id"]] + [b["id"] for b in bar_facts]
                                                  + [d["id"] for d in decisions]),
                           price_range=(min(prices), max(prices)), facts={
                               "signal": sig, "bars": bar_facts, "decisions": decisions, "kronos_forecasts": [],
                               "advisory_generated_at": now.isoformat()})


def estimate_review_cost(context: AdvisoryContext, *, pricing: dict, max_tokens: int,
                         typical_output_tokens: int = 350, frequencies=(1, 3, 6, 24, 96)) -> dict:
    """Token and USD estimate for one six-agent review (5 specialists + Coordinator) and for several review
    frequencies. Input tokens = prompt chars / CHARS_PER_TOKEN (conservative); output: typical and worst case
    (max_tokens per call; reasoning tokens, if a reasoning model is used, count as output and are NOT included in
    the typical figure)."""
    filler = {"assessment": "x" * 600, "evidence": ["bar:x"] * 5, "concerns": ["y" * 80] * 3, "confidence": 0.5,
              "recommendation": "caution", "valid_for_minutes": 60}
    per_role = {}
    for role in SPECIALISTS:
        system, user = _prompt(role, context)
        per_role[role] = estimate_tokens(system + user)
    system, user = _prompt("coordinator", context, {"validated_specialist_outputs": {r: filler for r in SPECIALISTS}})
    per_role["coordinator"] = estimate_tokens(system + user)
    tokens_in = sum(per_role.values())
    typical = call_cost_usd(tokens_in, typical_output_tokens * CALLS_PER_REVIEW, pricing)
    worst = call_cost_usd(tokens_in, max_tokens * CALLS_PER_REVIEW, pricing)
    return {"calls_per_review": CALLS_PER_REVIEW, "input_tokens_per_role": per_role,
            "input_tokens_per_review": tokens_in, "typical_output_tokens_per_call": typical_output_tokens,
            "max_tokens_per_call": max_tokens, "pricing_usd_per_million": pricing,
            "usd_per_review": {"typical": round(typical, 4), "worst_case": round(worst, 4)},
            "per_frequency": {f"{n}_reviews_per_day": {
                "calls_per_day": n * CALLS_PER_REVIEW,
                "usd_per_day": [round(n * typical, 2), round(n * worst, 2)],
                "usd_per_30_days": [round(30 * n * typical, 2), round(30 * n * worst, 2)]} for n in frequencies}}


def cmd_estimate_cost(args) -> dict:
    """Offline estimate (no AI call, no store written)."""
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    runner = config["runners"][args.runner]
    db = Path(args.db or runner["shadow_db"])
    ctx = representative_context(db, runner=args.runner, market=runner["market"], now=datetime.now(timezone.utc))
    out = {}
    for model, (pin, pout) in sorted(PRICE_SCENARIOS.items()):
        out[model] = estimate_review_cost(ctx, pricing={"input": pin, "output": pout},
                                          max_tokens=config["max_tokens"])
    return {"source_db": str(db), "bars_in_context": len(ctx.facts["bars"]), "decisions_in_context":
            len(ctx.facts["decisions"]), "price_source": PRICE_SOURCE, "estimates": out}


# Published list prices (USD per million tokens, < 200k-token prompts), docs.x.ai/docs/models, retrieved
# 2026-11-09. Planning figures only: re-check at activation; the config's pricing is what the guard enforces.
PRICE_SOURCE = "https://docs.x.ai/docs/models (retrieved 2026-11-09; re-verify at activation)"
PRICE_SCENARIOS = {"grok-4.20-0309-non-reasoning": (1.25, 2.50), "grok-4.3": (1.25, 2.50), "grok-4.7": (2.00, 6.00)}


def cmd_status(args) -> dict:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    path = Path(args.advisory_dir or config["advisory_dir"]) / "advisory.sqlite"
    return {"activated": config.get("activated"), "model": config.get("model"),
            "env_activation_flag": os.environ.get("GROK_AGENTS_ACTIVATED") == "1",
            "xai_key_present": bool(os.environ.get("XAI_API_KEY")),
            "store": AdvisoryStore(path).counts() if path.exists() else "empty"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("review")
    p.add_argument("--runner", required=True)
    p.add_argument("--db", default=None, help="shadow.sqlite to read (opened read-only)")
    p.add_argument("--signal-id", default=None)
    p.add_argument("--latest", type=int, default=3)
    p.add_argument("--transport", choices=("dry-run", "replay", "xai"), default="dry-run")
    p.add_argument("--replay-file", default=None)
    p.add_argument("--kronos-file", default=None, help="recorded Kronos forecasts {id: {...}} (optional)")
    p.add_argument("--i-have-activation-approval", action="store_true")
    p.set_defaults(func=cmd_review)
    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)
    e = sub.add_parser("estimate-cost", help="offline token/USD estimate; no AI call")
    e.add_argument("--runner", default="xau_orb")
    e.add_argument("--db", default=None, help="shadow.sqlite to size the context from (read-only)")
    e.set_defaults(func=cmd_estimate_cost)
    r = sub.add_parser("review-signals", help="review NEW candidate-strategy signals only (signal-triggered)")
    r.add_argument("--runner", default=None, help="one runner; default all configured runners")
    r.add_argument("--transport", choices=("dry-run", "xai"), default="dry-run")
    r.add_argument("--kronos-file", default=None)
    r.add_argument("--db", action="append", metavar="RUNNER=PATH",
                   help="shadow.sqlite of a runner in another checkout (opened read-only); repeatable")
    r.add_argument("--i-have-activation-approval", action="store_true")
    r.set_defaults(func=cmd_review_signals)
    v = sub.add_parser("verify-audit", help="verify the audit log hash chain against the store")
    v.set_defaults(func=cmd_verify_audit)
    for q in (p, s, e, r, v):
        q.add_argument("--config", default=DEFAULT_CONFIG)
        q.add_argument("--advisory-dir", default=None)
    args = parser.parse_args()
    print(json.dumps(args.func(args), indent=2, default=str))


if __name__ == "__main__":
    main()

