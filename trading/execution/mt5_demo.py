"""MetaTrader 5 DEMO execution adapter. Disabled until the operator approves a demo execution trial.

Safety layers (all must pass for every single order; any failure records the reason and sends nothing):
 1. Trial approval: an approval file in the adapter's own state directory (written by the operator) AND the
    environment variable KRONOS_MT5_DEMO_TRIAL equal to that approval's id. Approvals expire and name the exact
    account login, server, symbol and magic number.
 2. Demo only: account_info().trade_mode must be DEMO (0) at connect AND immediately before each order_send.
    Live/contest accounts raise LiveTradingForbidden. There is no live mode in this module.
 3. Deterministic risk: an APPROVED `trading.risk.RiskDecision` is required. The adapter re-checks the loss at the
    stop with the terminal's own `order_calc_profit` at the actual fill price, plus commission, and rejects if it
    exceeds 0.5% of current equity. It never increases size; volume is rounded DOWN to the broker step.
 4. Protection: every order carries both stop-loss and take-profit, on the correct sides and outside the broker's
    stops level. After a fill the broker position is checked for SL/TP; if missing the adapter halts.
 5. Duplicates: idempotency key = sha256(signal_id|book)[:16]; recorded in the ledger BEFORE sending, and the broker
    is searched (positions, orders, recent deals) for the key's comment. One order per signal, no automatic re-send.
 6. Reconciliation: ledger vs broker positions for this magic number; any mismatch writes HALT (blocks new orders).
 7. Controls: KILL, STOP or HALT in the state directory block new orders. Daily order cap from the approval.
 8. Connection: a disconnected terminal fails closed; after a reconnect a reconciliation must pass first.
There is no martingale, no averaging down, no retry loop and no dashboard entry point.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from trading.common import Side, ValidationError
from trading.config import LiveTradingForbidden, verify_mt5_demo_account
from trading.connectors.mt5 import READ_ONLY_FUNCTIONS, Mt5Unavailable, _as_dict
from trading.risk import RiskDecision
from trading.signals import TradeSignal

UTC = timezone.utc
MAX_RISK_FRACTION = Decimal("0.005")
APPROVAL_FILE = "TRIAL_APPROVAL.json"
APPROVAL_ENV = "KRONOS_MT5_DEMO_TRIAL"
TRADING_FUNCTIONS = frozenset({"order_check", "order_send"})
# MT5 constants (values from the MetaTrader5 package; repeated so the module is testable without it)
TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC = 1, 0, 1, 0
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
RET_DONE, RET_PLACED, RET_DONE_PARTIAL = 10009, 10008, 10010
RETCODES = {10004: "requote", 10006: "rejected", 10007: "cancelled", 10008: "placed", 10009: "done",
            10010: "done_partial", 10011: "error", 10012: "timeout", 10013: "invalid_request",
            10014: "invalid_volume", 10015: "invalid_price", 10016: "invalid_stops", 10017: "trade_disabled",
            10018: "market_closed", 10019: "no_money", 10020: "price_changed", 10021: "price_off",
            10024: "too_many_requests", 10026: "server_disables_autotrading", 10027: "client_disables_autotrading",
            10031: "no_connection", 10033: "orders_limit", 10034: "volume_limit"}


class ExecutionNotAuthorized(PermissionError):
    """The demo execution trial is not approved (or a safety layer blocks this order)."""


class ExecutionRejected(ValidationError):
    """The order failed a pre-send validation; nothing was sent."""


def idempotency_key(signal_id: str, book: str) -> str:
    return hashlib.sha256(f"{signal_id}|{book}".encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- approval

@dataclass(frozen=True)
class TrialApproval:
    approval_id: str
    approved_by: str
    approved_at: str
    expires_at: str
    account_login: int
    server: str
    symbol: str
    magic: int
    max_orders_per_day: int
    max_open_positions: int = 1

    @classmethod
    def load(cls, state_dir: Path) -> "TrialApproval | None":
        path = Path(state_dir) / APPROVAL_FILE
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def problems(self, *, now: datetime, env: dict, account: dict, symbol: str, magic: int) -> list[str]:
        out = []
        if env.get(APPROVAL_ENV) != self.approval_id:
            out.append(f"{APPROVAL_ENV} does not match the approval id")
        if now >= datetime.fromisoformat(self.expires_at):
            out.append("trial approval expired")
        if account.get("login") != self.account_login or account.get("server") != self.server:
            out.append("account login/server differ from the approval")
        if symbol != self.symbol or magic != self.magic:
            out.append("symbol/magic differ from the approval")
        if self.max_orders_per_day < 1 or self.max_open_positions != 1:
            out.append("approval limits invalid (one open position, at least one order per day)")
        return out


# --------------------------------------------------------------------------- execution ledger

class ExecutionLedger:
    """Append-mostly SQLite record of every intent, check, send, result, reconciliation and halt."""

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS orders (key TEXT PRIMARY KEY, signal_id TEXT NOT NULL, book TEXT NOT NULL, "
        "strategy TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, side TEXT, "
        "volume TEXT, price TEXT, sl TEXT, tp TEXT, planned_risk TEXT, request TEXT, check_result TEXT, "
        "result TEXT, retcode INTEGER, order_ticket INTEGER, deal_ticket INTEGER, position_id INTEGER, "
        "reason TEXT, net_result TEXT, closed_at TEXT)",
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, "
        "detail TEXT NOT NULL)",
    )

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        for s in self.SCHEMA:
            self._db.execute(s)

    def get(self, key: str) -> dict | None:
        cur = self._db.execute("SELECT * FROM orders WHERE key=?", (key,))
        row = cur.fetchone()
        return None if row is None else dict(zip([c[0] for c in cur.description], row))

    def insert_intent(self, key: str, *, signal_id: str, book: str, strategy: str, now: datetime) -> bool:
        with self._lock:
            try:
                self._db.execute("INSERT INTO orders (key, signal_id, book, strategy, state, created_at, updated_at) "
                                 "VALUES (?,?,?,?,?,?,?)", (key, signal_id, book, strategy, "intent", now.isoformat(),
                                                             now.isoformat()))
                return True
            except sqlite3.IntegrityError:
                return False  # duplicate: this signal/book already has a record

    def update(self, key: str, now: datetime, **fields) -> None:
        enc = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else (str(v) if isinstance(v, Decimal) else v))
               for k, v in fields.items()}
        cols = ", ".join(f"{k}=?" for k in enc)
        with self._lock:
            self._db.execute(f"UPDATE orders SET {cols}, updated_at=? WHERE key=?", (*enc.values(), now.isoformat(), key))

    def event(self, now: datetime, kind: str, **detail) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events (at, kind, detail) VALUES (?,?,?)",
                             (now.isoformat(), kind, json.dumps(detail, default=str)))

    def rows(self, state: str | None = None) -> list[dict]:
        cur = self._db.execute("SELECT * FROM orders" + (" WHERE state=?" if state else "") + " ORDER BY created_at",
                               (state,) if state else ())
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def sent_today(self, now: datetime) -> int:
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self._db.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ? AND state NOT IN ('intent', 'blocked', "
                                "'validation_rejected', 'duplicate')", (day,)).fetchone()[0]

    def close(self) -> None:
        self._db.close()


# --------------------------------------------------------------------------- MT5 proxy for the demo trial

class DemoTradingMt5:
    """Read-only allowlist plus order_check/order_send, the latter only while armed by a gated submit and only after
    re-verifying that the account is DEMO immediately before the call."""

    def __init__(self, module):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_armed", False)

    def __getattr__(self, name: str):
        if name.isupper():
            return getattr(self._module, name)
        if name in READ_ONLY_FUNCTIONS or name == "order_check":
            return getattr(self._module, name)
        if name == "order_send":
            return self._guarded_send
        raise PermissionError(f"MT5 function {name!r} is not permitted by the demo execution adapter")

    def __setattr__(self, name, value):
        raise PermissionError("DemoTradingMt5 is immutable")

    def _arm(self, value: bool) -> None:
        object.__setattr__(self, "_armed", value)

    def _guarded_send(self, request: dict):
        if not self._armed:
            raise ExecutionNotAuthorized("order_send called outside a gated submission")
        verify_mt5_demo_account(_as_dict(self._module.account_info()).get("trade_mode"))
        return self._module.order_send(request)


# --------------------------------------------------------------------------- adapter

def _round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    steps = (price / tick).to_integral_value(rounding="ROUND_CEILING" if up else "ROUND_FLOOR")
    return steps * tick


@dataclass
class SymbolRules:
    name: str
    point: Decimal
    tick_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    stops_level_points: int
    freeze_level_points: int
    filling_flags: int
    digits: int

    @classmethod
    def from_info(cls, info: dict) -> "SymbolRules":
        return cls(name=info["name"], point=Decimal(str(info["point"])),
                   tick_size=Decimal(str(info.get("trade_tick_size") or info["point"])),
                   volume_min=Decimal(str(info["volume_min"])), volume_max=Decimal(str(info["volume_max"])),
                   volume_step=Decimal(str(info["volume_step"])), stops_level_points=int(info.get("trade_stops_level") or 0),
                   freeze_level_points=int(info.get("trade_freeze_level") or 0), filling_flags=int(info.get("filling_mode") or 0),
                   digits=int(info.get("digits") or 2))


def build_request(signal: TradeSignal, decision: RiskDecision, rules: SymbolRules, tick: dict, *, key: str,
                  magic: int, deviation_points: int = 20) -> dict:
    """Pure: an MT5 market-order request with mandatory SL and TP, or ExecutionRejected. Never increases size."""
    if not decision.approved or decision.signal_id != signal.signal_id:
        raise ExecutionRejected("no approved risk decision for this signal")
    if signal.take_profit is None:
        raise ExecutionRejected("take-profit is mandatory")
    long = signal.side is Side.LONG
    price = Decimal(str(tick["ask"] if long else tick["bid"]))
    if price <= 0:
        raise ExecutionRejected("no valid quote")
    volume = (decision.size / rules.volume_step).to_integral_value(rounding=ROUND_DOWN) * rules.volume_step
    if volume < rules.volume_min:
        raise ExecutionRejected(f"volume {decision.size} below the broker minimum {rules.volume_min}")
    if volume > rules.volume_max:
        raise ExecutionRejected(f"volume {volume} above the broker maximum {rules.volume_max}")
    sl = _round_to_tick(signal.stop_price, rules.tick_size, up=not long)       # away from entry
    tp = _round_to_tick(signal.take_profit, rules.tick_size, up=not long)      # toward entry
    if (long and not (sl < price < tp)) or (not long and not (tp < price < sl)):
        raise ExecutionRejected("stop/target on the wrong side of the current price")
    min_dist = rules.point * max(rules.stops_level_points, rules.freeze_level_points)
    if abs(price - sl) <= min_dist or abs(tp - price) <= min_dist:
        raise ExecutionRejected(f"stop or target within the broker stops level ({min_dist})")
    filling = (ORDER_FILLING_FOK if rules.filling_flags & 1 else ORDER_FILLING_IOC if rules.filling_flags & 2
               else ORDER_FILLING_RETURN)
    return {"action": TRADE_ACTION_DEAL, "symbol": rules.name, "volume": float(volume),
            "type": ORDER_TYPE_BUY if long else ORDER_TYPE_SELL, "price": float(price), "sl": float(sl),
            "tp": float(tp), "deviation": int(deviation_points), "magic": int(magic), "comment": f"kr:{key}",
            "type_time": ORDER_TIME_GTC, "type_filling": filling}


class Mt5DemoExecutor:
    def __init__(self, module, state_dir: Path, *, symbol: str, magic: int, env: dict | None = None,
                 clock=_now, max_reconnects: int = 3):
        self.mt5 = DemoTradingMt5(module)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ExecutionLedger(self.state_dir / "execution.sqlite")
        self.symbol, self.magic = symbol, magic
        self.env = dict(os.environ if env is None else env)
        self.clock = clock
        self.connected = False
        self.needs_reconcile = True
        self.max_reconnects = max_reconnects

    # -- connection ----------------------------------------------------------------------------
    def connect(self) -> dict:
        if not self.mt5.initialize():
            self.connected = False
            raise Mt5Unavailable(f"MT5 terminal not available: {self.mt5.last_error()}")
        account, terminal = _as_dict(self.mt5.account_info()), _as_dict(self.mt5.terminal_info())
        try:
            verify_mt5_demo_account(account.get("trade_mode"))
        except LiveTradingForbidden:
            self.mt5.shutdown()
            raise
        if not terminal.get("connected"):
            raise Mt5Unavailable("terminal not connected to the broker")
        self.connected, self.needs_reconcile = True, True
        self.ledger.event(self.clock(), "connected", login=account.get("login"), server=account.get("server"))
        return account

    def ensure_connected(self) -> bool:
        """Fail closed: report disconnection, try a bounded number of reconnects, and require reconciliation."""
        try:
            if self.connected and _as_dict(self.mt5.terminal_info()).get("connected"):
                return True
        except Exception:  # noqa: BLE001
            pass
        if self.connected:
            self.ledger.event(self.clock(), "disconnected")
        for attempt in range(1, self.max_reconnects + 1):
            try:
                self.connect()
                self.ledger.event(self.clock(), "reconnected", attempt=attempt)
                return True
            except (Mt5Unavailable, LiveTradingForbidden) as exc:
                self.ledger.event(self.clock(), "reconnect_failed", attempt=attempt, error=str(exc)[:200])
        return False

    # -- gates ----------------------------------------------------------------------------------
    def gate_problems(self, account: dict) -> list[str]:
        now = self.clock()
        out = []
        for flag in ("KILL", "STOP", "HALT"):
            if (self.state_dir / flag).exists():
                out.append(f"{flag} present")
        approval = TrialApproval.load(self.state_dir)
        if approval is None:
            out.append("demo execution trial not approved (no approval file)")
        else:
            out += approval.problems(now=now, env=self.env, account=account, symbol=self.symbol, magic=self.magic)
            if self.ledger.sent_today(now) >= approval.max_orders_per_day:
                out.append("daily order cap reached")
            if len(self._broker_positions()) >= approval.max_open_positions:
                out.append("maximum open positions reached")
        if not self.connected:
            out.append("MT5 not connected")
        if self.needs_reconcile:
            out.append("reconciliation required before trading")
        if account.get("trade_mode") != 0:
            out.append("account is not a demo account")
        if not account.get("trade_allowed", False):
            out.append("trading not allowed on this account/terminal")
        return out

    def _broker_positions(self) -> list[dict]:
        return [_as_dict(p) for p in (self.mt5.positions_get(symbol=self.symbol) or ()) if _as_dict(p).get("magic") == self.magic]

    def _broker_has_key(self, key: str) -> bool:
        tag = f"kr:{key}"
        now = self.clock()
        pos = [_as_dict(p) for p in (self.mt5.positions_get(symbol=self.symbol) or ())]
        orders = [_as_dict(o) for o in (self.mt5.orders_get(symbol=self.symbol) or ())]
        deals = [_as_dict(d) for d in (self.mt5.history_deals_get(now - timedelta(days=7), now + timedelta(minutes=5)) or ())]
        return any(x.get("comment", "").startswith(tag) for x in pos + orders + deals)

    # -- submission -----------------------------------------------------------------------------
    def submit(self, signal: TradeSignal, decision: RiskDecision, *, book: str, strategy: str) -> dict:
        now = self.clock()
        key = idempotency_key(signal.signal_id, book)
        if not self.ledger.insert_intent(key, signal_id=signal.signal_id, book=book, strategy=strategy, now=now):
            return {**(self.ledger.get(key) or {}), "key": key, "state": "duplicate"}
        try:
            if not self.ensure_connected():
                raise ExecutionNotAuthorized("MT5 disconnected (fail closed)")
            account = _as_dict(self.mt5.account_info())
            problems = self.gate_problems(account)
            if problems:
                raise ExecutionNotAuthorized("; ".join(problems))
            if self._broker_has_key(key):
                self.ledger.update(key, now, state="duplicate", reason="broker already has this key")
                return {"key": key, "state": "duplicate"}
            rules = SymbolRules.from_info(_as_dict(self.mt5.symbol_info(self.symbol)))
            tick = _as_dict(self.mt5.symbol_info_tick(self.symbol))
            request = build_request(signal, decision, rules, tick, key=key, magic=self.magic)
            self._check_risk(request, account)
        except ExecutionNotAuthorized as exc:
            self.ledger.update(key, now, state="blocked", reason=str(exc)[:500])
            raise
        except (ExecutionRejected, ValidationError, Mt5Unavailable) as exc:
            self.ledger.update(key, now, state="validation_rejected", reason=str(exc)[:500])
            return {"key": key, "state": "validation_rejected", "reason": str(exc)}
        self.ledger.update(key, now, state="checking", request=request, side=signal.side.value,
                           volume=request["volume"], price=request["price"], sl=request["sl"], tp=request["tp"])
        check = _as_dict(self.mt5.order_check(request))
        if check.get("retcode") not in (0, None):
            self.ledger.update(key, now, state="check_rejected", check_result=check, retcode=check.get("retcode"),
                               reason=check.get("comment"))
            return {"key": key, "state": "check_rejected", "retcode": check.get("retcode")}
        self.ledger.update(key, now, state="sending", check_result=check)
        self.mt5._arm(True)
        try:
            result = _as_dict(self.mt5.order_send(request))
        except Exception as exc:  # noqa: BLE001 - outcome unknown: must reconcile before anything else
            self.needs_reconcile = True
            self.ledger.update(key, self.clock(), state="unknown", reason=f"{type(exc).__name__}: {exc}"[:500])
            raise
        finally:
            self.mt5._arm(False)
        code = result.get("retcode")
        state = "filled" if code in (RET_DONE, RET_DONE_PARTIAL) else "accepted" if code == RET_PLACED else "rejected"
        self.ledger.update(key, self.clock(), state=state, result=result, retcode=code, order_ticket=result.get("order"),
                           deal_ticket=result.get("deal"), reason=RETCODES.get(code, str(code)))
        self.ledger.event(self.clock(), f"order_{state}", key=key, retcode=code)
        if state == "filled":
            self._verify_protection(key)
        return {"key": key, "state": state, "retcode": code, "reason": RETCODES.get(code)}

    def _check_risk(self, request: dict, account: dict) -> None:
        """Loss at the stop from the terminal's own profit calculation, plus commission, <= 0.5% of equity."""
        equity = Decimal(str(account.get("equity") or 0))
        if equity <= 0:
            raise ExecutionRejected("account equity unavailable")
        loss = self.mt5.order_calc_profit(request["type"], request["symbol"], request["volume"], request["price"], request["sl"])
        if loss is None:
            raise ExecutionRejected("terminal could not compute the loss at the stop (fail closed)")
        commission = Decimal(str(self.env.get("KRONOS_MT5_COMMISSION_PER_LOT_RT", "7"))) * Decimal(str(request["volume"]))
        risk = abs(Decimal(str(loss))) + commission
        if risk > equity * MAX_RISK_FRACTION:
            raise ExecutionRejected(f"loss at stop {risk:.2f} exceeds 0.5% of equity ({equity * MAX_RISK_FRACTION:.2f})")

    def _verify_protection(self, key: str) -> None:
        tag = f"kr:{key}"
        pos = [p for p in self._broker_positions() if p.get("comment", "").startswith(tag)]
        if not pos or not pos[0].get("sl") or not pos[0].get("tp"):
            self._halt("filled position without broker stop-loss/take-profit", key=key)
        else:
            self.ledger.update(key, self.clock(), position_id=pos[0].get("ticket"))

    def _halt(self, reason: str, **detail) -> None:
        (self.state_dir / "HALT").write_text(json.dumps({"at": self.clock().isoformat(), "reason": reason, **detail}),
                                             encoding="utf-8")
        self.ledger.event(self.clock(), "halt", reason=reason, **detail)

    # -- reconciliation -------------------------------------------------------------------------
    def reconcile(self) -> dict:
        """Ledger 'filled' orders without a close vs broker positions with this magic. Mismatch -> HALT."""
        now = self.clock()
        broker = {p.get("comment", "")[3:19]: p for p in self._broker_positions() if p.get("comment", "").startswith("kr:")}
        foreign = [p.get("ticket") for p in self._broker_positions() if not p.get("comment", "").startswith("kr:")]
        open_ledger = {r["key"]: r for r in self.ledger.rows("filled")}
        missing_at_broker = [k for k in open_ledger if k not in broker]
        unknown_at_broker = [k for k in broker if k not in open_ledger]
        unknown_state = [r["key"] for r in self.ledger.rows("unknown") + self.ledger.rows("sending")]
        # a ledger position missing at the broker has closed (SL/TP): record the close from deal history;
        # one that is missing AND has no closing deals is a mismatch
        closed = self._record_closes(missing_at_broker, now)
        vanished = [k for k in missing_at_broker if k not in closed]
        ok = not (unknown_at_broker or foreign or unknown_state or vanished)
        report = {"ok": ok, "broker_positions": len(broker), "ledger_open": len(open_ledger), "closed_now": closed,
                  "unknown_at_broker": unknown_at_broker, "foreign_positions_same_magic": foreign,
                  "unresolved_sends": unknown_state, "missing_without_close": vanished}
        self.ledger.event(now, "reconciliation", **report)
        if ok:
            self.needs_reconcile = False
        else:
            self._halt("reconciliation mismatch", **report)
        return report

    def _record_closes(self, keys: list[str], now: datetime) -> list[str]:
        from trading.accounting import positions_from_deals
        deals = [_as_dict(d) for d in (self.mt5.history_deals_get(now - timedelta(days=30), now + timedelta(minutes=5)) or ())]
        closed, _ = positions_from_deals([d for d in deals if d.get("magic") == self.magic])
        by_comment = {c.meta.get("comment", "")[3:19]: c for c in closed}
        done = []
        for k in keys:
            c = by_comment.get(k)
            if c is not None:
                self.ledger.update(k, now, state="closed", net_result=str(c.net), closed_at=c.closed_at.isoformat(),
                                   reason=c.exit_reason)
                done.append(k)
        return done
