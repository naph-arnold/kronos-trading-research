"""Read-only MT5 account recorder for the Command Center's broker view.

    python -m research.mt5_account_monitor --once                 # one snapshot
    python -m research.mt5_account_monitor --interval 60          # lightweight polling (Ctrl+C / STOP to end)

Uses the existing read-only allowlist proxy (`Mt5ReadOnlyConnector`): account_info, terminal_info, positions_get
and history_deals_get only. It cannot place, modify or close anything. It refuses non-demo accounts. Each poll
records a snapshot (or the reason it could not), so the dashboard can show "disconnected" instead of a stale or
zero balance. State: its own directory (default state/mt5_account), never a runner's.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
HISTORY_START = datetime(2020, 1, 1, tzinfo=UTC)
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS snapshots (at TEXT PRIMARY KEY, connected INTEGER NOT NULL, account TEXT, "
    "terminal TEXT, positions INTEGER, error TEXT)",
    "CREATE TABLE IF NOT EXISTS positions (at TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS deals (ticket INTEGER PRIMARY KEY, time INTEGER NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
ACCOUNT_FIELDS = ("login", "server", "company", "currency", "trade_mode", "leverage", "balance", "equity", "profit",
                  "margin", "margin_free", "margin_level", "credit", "trade_allowed", "limit_orders")


def _dict(x) -> dict:
    return {} if x is None else dict(x._asdict()) if hasattr(x, "_asdict") else dict(x)


class AccountStore:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        for s in SCHEMA:
            self.db.execute(s)

    def record(self, at: datetime, *, account=None, terminal=None, positions=None, deals=None, error=None) -> None:
        self.db.execute("BEGIN")
        try:
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                            (at.isoformat(), int(error is None), json.dumps(account) if account else None,
                             json.dumps(terminal) if terminal else None,
                             None if positions is None else len(positions), error))
            if positions is not None:
                self.db.execute("DELETE FROM positions")
                self.db.executemany("INSERT INTO positions VALUES (?,?)", [(at.isoformat(), json.dumps(p)) for p in positions])
            for d in deals or ():
                self.db.execute("INSERT OR REPLACE INTO deals VALUES (?,?,?)", (int(d["ticket"]), int(d["time"]), json.dumps(d)))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise


def snapshot(store: AccountStore, *, module=None, now: datetime | None = None) -> dict:
    """One read-only snapshot. Any failure is recorded as a disconnected snapshot (never as zero balances)."""
    from trading.connectors.mt5 import Mt5ReadOnlyConnector, Mt5Unavailable
    from trading.config import LiveTradingForbidden
    from trading.clock import ServerTimeZone

    now = now or datetime.now(UTC)
    conn = Mt5ReadOnlyConnector(server_tz=ServerTimeZone("UTC", timedelta(0)), module=module)
    try:
        conn.connect()  # refuses non-demo accounts and disconnected terminals
        mt5 = conn.mt5
        account = {k: v for k, v in _dict(mt5.account_info()).items() if k in ACCOUNT_FIELDS}
        terminal = {k: v for k, v in _dict(mt5.terminal_info()).items() if k in ("connected", "trade_allowed", "build", "ping_last")}
        positions = [_dict(p) for p in (mt5.positions_get() or ())]
        deals = [_dict(d) for d in (mt5.history_deals_get(HISTORY_START, now + timedelta(days=2)) or ())]
        store.record(now, account=account, terminal=terminal, positions=positions, deals=deals)
        return {"connected": True, "balance": account.get("balance"), "equity": account.get("equity"),
                "positions": len(positions), "deals": len(deals)}
    except (Mt5Unavailable, LiveTradingForbidden, OSError, RuntimeError) as exc:
        store.record(now, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return {"connected": False, "error": f"{type(exc).__name__}"}
    finally:
        conn.shutdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default="state/mt5_account")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--interval", type=int, help="seconds between snapshots (>= 30)")
    args = ap.parse_args(argv)
    state = Path(args.state_dir)
    store = AccountStore(state / "account.sqlite")
    if args.once:
        print(json.dumps(snapshot(store)))
        return 0
    interval = max(30, args.interval)
    while not (state / "STOP").exists():
        print(json.dumps(snapshot(store)), flush=True)
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
