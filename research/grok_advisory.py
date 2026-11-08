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
       python -m research.grok_advisory status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from trading.agents.desk import (PROVIDER, ROLE_BRIEFS, SPECIALISTS, AdvisoryContext, RoleOutput,
                                 deterministic_coordinator_fallback, output_schema, parse_role_output)
from trading.agents.mtf_advisory import OFFICIAL_XAI_BASE_URL, GrokNotActivated
from trading.agents.news import ALLOWED_KEYS as NEWS_KEYS
from trading.agents.news import parse_news_assessment
from trading.common import ValidationError

DEFAULT_CONFIG = "research/configs/grok_advisory.json"
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

    def complete(self, role: str, system: str, user: str) -> str:
        key = os.environ.get(self.key_env, "")
        if not key:
            raise GrokNotActivated(f"{self.key_env} is not set")
        body = {"model": self.model, "temperature": 0, "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(self.base_url + CHAT_PATH, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise GrokNotActivated(f"xAI request failed: {type(exc).__name__}: {str(exc).replace(key, '***')}") from None
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise GrokNotActivated("xAI response missing choices[0].message.content") from None


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
    return XaiTransport(model=config.get("model") or "", base_url=config["base_url"],
                        timeout_s=config["timeout_s"], max_tokens=config["max_tokens"])


# --------------------------------------------------------------------------- advisory store (separate)

class AdvisoryStore:
    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, role TEXT NOT NULL,"
        " subject TEXT NOT NULL, transport TEXT NOT NULL, model TEXT, context_sha256 TEXT NOT NULL,"
        " prompt_sha256 TEXT NOT NULL, response_sha256 TEXT, status TEXT NOT NULL, error TEXT)",
        "CREATE TABLE IF NOT EXISTS outputs (subject TEXT NOT NULL, role TEXT NOT NULL, at TEXT NOT NULL,"
        " valid INTEGER NOT NULL, payload TEXT, error TEXT, PRIMARY KEY (subject, role, at))",
        "CREATE TABLE IF NOT EXISTS summaries (subject TEXT NOT NULL, at TEXT NOT NULL, runner TEXT NOT NULL,"
        " context_sha256 TEXT NOT NULL, recommendation TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (subject, at))",
    )

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        for stmt in self.SCHEMA:
            self.db.execute(stmt)
        self.db.commit()

    def calls_since(self, since: datetime, transport: str = "xai") -> int:
        return self.db.execute("SELECT COUNT(*) FROM calls WHERE at >= ? AND transport = ?",
                               (since.isoformat(), transport)).fetchone()[0]

    def call(self, **row) -> None:
        self.db.execute("INSERT INTO calls (at, role, subject, transport, model, context_sha256, prompt_sha256,"
                        " response_sha256, status, error) VALUES (:at, :role, :subject, :transport, :model,"
                        " :context_sha256, :prompt_sha256, :response_sha256, :status, :error)", row)
        self.db.commit()

    def output(self, subject: str, role: str, at: str, valid: bool, payload, error: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO outputs VALUES (?, ?, ?, ?, ?, ?)",
                        (subject, role, at, int(valid), None if payload is None else json.dumps(payload, default=str),
                         error))
        self.db.commit()

    def summary(self, subject: str, at: str, runner: str, context_sha: str, body: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?, ?)",
                        (subject, at, runner, context_sha, body["recommendation"], json.dumps(body, default=str)))
        self.db.commit()

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
                       "reasons": json.loads(d[2])} for d in decisions]
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
             model: str | None, event_store=None, daily_call_budget: int = 0) -> dict:
    """Ask the five specialists, then the Coordinator. Every output is validated; failures are recorded and the
    Coordinator falls back to `insufficient_inputs`. Returns the recorded summary (advisory only)."""
    context_sha = context.digest()
    at = now.isoformat()
    valid: dict[str, RoleOutput] = {}
    errors: dict[str, str] = {}

    def ask(role: str, extra: dict | None = None):
        system, user = _prompt(role, context, extra)
        row = {"at": at, "role": role, "subject": context.subject_id, "transport": transport.name, "model": model,
               "context_sha256": context_sha, "prompt_sha256": hashlib.sha256((system + user).encode()).hexdigest(),
               "response_sha256": None, "status": "ok", "error": None}
        if transport.name == "xai":
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if store.calls_since(day) >= daily_call_budget:
                row.update(status="skipped", error="daily call budget reached")
                store.call(**row)
                return None, "daily call budget reached"
        try:
            text = transport.complete(role, system, user)
        except GrokNotActivated as exc:
            row.update(status="unavailable", error=str(exc)[:300])
            store.call(**row)
            return None, f"unavailable: {exc}"[:300]
        row["response_sha256"] = hashlib.sha256(text.encode()).hexdigest()
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
                                event_store=events, daily_call_budget=config["daily_call_budget"]))
        if events is not None:
            events._db.close()
    return {"transport": transport.name, "reviewed": len(out),
            "recommendations": [s["recommendation"] for s in out], "store": store.counts()}


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
    for q in (p, s):
        q.add_argument("--config", default=DEFAULT_CONFIG)
        q.add_argument("--advisory-dir", default=None)
    args = parser.parse_args()
    print(json.dumps(args.func(args), indent=2, default=str))


if __name__ == "__main__":
    main()

