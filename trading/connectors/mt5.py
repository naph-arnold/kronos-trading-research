"""Read-only MetaTrader 5 connector for XAUUSD research (Pipeline A).

Safety model:
* The MetaTrader5 module is wrapped in `ReadOnlyMt5`, an allowlist proxy. Any
  function not on the list (including every order/position-changing call) raises
  `PermissionError`; there is no code path that can trade.
* After connecting, the account must report ACCOUNT_TRADE_MODE_DEMO, otherwise
  the connector shuts the terminal connection down and raises.
* Credentials are optional (an already logged-in demo terminal can be used),
  are never logged, and are never stored in results.

Timestamps: MT5 `time` fields are broker *server* wall time. They are converted
with a configured `ServerTimeZone`, which must first be verified (fresh tick and/or
session-structure check). Raw server times are preserved alongside UTC.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading.clock import ServerClockError, ServerTimeZone, verify_server_offset
from trading.common import ValidationError, to_decimal
from trading.config import LiveTradingForbidden, verify_mt5_demo_account
from trading.instruments import Mt5InstrumentSpec
from trading.marketdata import Bar, Quote

READ_ONLY_FUNCTIONS = frozenset({
    "initialize", "shutdown", "last_error", "version", "terminal_info", "account_info",
    "symbols_get", "symbols_total", "symbol_info", "symbol_info_tick", "symbol_select",
    "copy_rates_range", "copy_rates_from_pos", "copy_rates_from", "copy_ticks_range",
    "order_calc_margin", "order_calc_profit", "positions_get", "positions_total", "orders_get", "orders_total",
    # Account history (read-only): the only verifiable source of the commission actually charged per deal.
    "history_deals_get", "history_deals_total", "history_orders_get", "history_orders_total",
})

TIMEFRAMES = {"1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
              "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1"}
SYMBOL_TRADE_MODES = {0: "disabled", 1: "long_only", 2: "short_only", 3: "close_only", 4: "full"}
ORDER_MODE_FLAGS = {1: "market", 2: "limit", 4: "stop", 8: "stop_limit", 16: "sl", 32: "tp", 64: "close_by"}
FILLING_FLAGS = {1: "fok", 2: "ioc", 4: "boc"}
_XAU_NAME = re.compile(r"^XAUUSD[._\-a-zA-Z0-9]{0,6}$")


class Mt5Unavailable(RuntimeError):
    """MetaTrader5 package/terminal missing, or the terminal is disconnected."""


class ReadOnlyMt5:
    """Allowlist proxy over the MetaTrader5 module. Constants (UPPER_CASE) pass through."""

    def __init__(self, module):
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str):
        if name.isupper() or name.startswith(("TIMEFRAME_", "ORDER_TYPE_", "ACCOUNT_", "SYMBOL_")):
            return getattr(self._module, name)
        if name not in READ_ONLY_FUNCTIONS:
            raise PermissionError(f"MT5 function {name!r} is not permitted in read-only research mode")
        return getattr(self._module, name)

    def __setattr__(self, name, value):
        raise PermissionError("ReadOnlyMt5 is immutable")


def _as_dict(record) -> dict:
    if record is None:
        return {}
    if hasattr(record, "_asdict"):
        return dict(record._asdict())
    return dict(record)


@dataclass
class SpecDiscovery:
    """Broker-reported XAUUSD specification with explicit verification status."""

    symbol: str
    retrieved_at: str
    fields: dict
    missing: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return not self.missing and not self.problems

    def to_instrument_spec(self, *, commission_assumption: Decimal | None = None) -> Mt5InstrumentSpec:
        """Only a fully verified discovery may become a risk-engine instrument spec.

        Research exception: if the *only* missing item is the commission (MT5 does not expose it),
        an explicit `commission_assumption` may be supplied. The discovery stays unverified, so any
        result built on it must be classified experimental."""
        commission_only = (self.missing and all(m.startswith("commission_per_lot_round_trip") for m in self.missing)
                           and not self.problems)
        if not self.verified and not (commission_only and commission_assumption is not None):
            raise ValidationError(f"Broker specification unverified: missing={self.missing} problems={self.problems}")
        f = dict(self.fields)
        if f.get("commission_per_lot_round_trip") is None:
            f["commission_per_lot_round_trip"] = commission_assumption
        spec = Mt5InstrumentSpec(
            symbol="XAUUSD", broker_symbol=self.symbol, account_currency=f["account_currency"],
            contract_size=to_decimal(f["contract_size"], "contract_size", positive=True),
            point=to_decimal(f["point"], "point", positive=True),
            tick_size=to_decimal(f["tick_size"], "tick_size", positive=True),
            tick_value_loss=to_decimal(f.get("tick_value_loss_used", f["tick_value_loss"]), "tick_value_loss",
                                       positive=True),
            volume_min=to_decimal(f["volume_min"], "volume_min", positive=True),
            volume_max=to_decimal(f["volume_max"], "volume_max", positive=True),
            volume_step=to_decimal(f["volume_step"], "volume_step", positive=True),
            stops_level_points=int(f["stops_level_points"]),
            margin_per_lot=to_decimal(f["margin_per_lot"], "margin_per_lot", positive=True),
            commission_per_lot_round_trip=to_decimal(f["commission_per_lot_round_trip"], "commission",
                                                     nonnegative=True),
        )
        spec.validate()
        return spec

    def to_record(self) -> dict:
        return {"symbol": self.symbol, "retrieved_at": self.retrieved_at, "verified": self.verified,
                "fields": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.fields.items()},
                "missing": self.missing, "problems": self.problems}


@dataclass(frozen=True)
class Mt5Bar:
    bar: Bar
    server_time: int  # original MT5 `time` (server wall time as epoch seconds)
    spread_points: int
    tick_volume: int


class Mt5ReadOnlyConnector:
    def __init__(self, *, server_tz: ServerTimeZone, module=None, terminal_path: str | None = None,
                 login: int | None = None, password: str | None = None, server: str | None = None,
                 timeout_ms: int = 30_000, commission_per_lot_round_trip: Decimal | None = None):
        self.server_tz = server_tz
        self._module = module
        self._terminal_path, self._login, self._password, self._server = terminal_path, login, password, server
        self._timeout_ms = timeout_ms
        self._commission = commission_per_lot_round_trip  # MT5 does not expose commission per symbol
        self.mt5: ReadOnlyMt5 | None = None
        self.connected = False

    def __repr__(self) -> str:  # never leak credentials
        return f"Mt5ReadOnlyConnector(server_tz={self.server_tz.name!r}, connected={self.connected})"

    # -- connection --------------------------------------------------------------------
    @staticmethod
    def detect() -> dict:
        """Is the MetaTrader5 Python package importable? (Terminal presence is checked by connect.)"""
        try:
            import MetaTrader5 as module  # noqa: N813 - official package name
        except ImportError as exc:
            return {"package_installed": False, "error": str(exc)}
        return {"package_installed": True, "package_version": getattr(module, "__version__", "unknown")}

    def connect(self) -> dict:
        if self._module is None:
            try:
                import MetaTrader5 as module  # noqa: N813
            except ImportError as exc:
                raise Mt5Unavailable("MetaTrader5 package not installed (Windows only)") from exc
            self._module = module
        self.mt5 = ReadOnlyMt5(self._module)
        kwargs = {"timeout": self._timeout_ms}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self._login is not None:
            kwargs.update(login=int(self._login), password=self._password or "", server=self._server or "")
        if not self.mt5.initialize(**kwargs):
            raise Mt5Unavailable(f"MT5 terminal not available: {self.mt5.last_error()}")
        try:
            account = _as_dict(self.mt5.account_info())
            terminal = _as_dict(self.mt5.terminal_info())
            if not account:
                raise Mt5Unavailable(f"No account information: {self.mt5.last_error()}")
            verify_mt5_demo_account(account.get("trade_mode"))
            if not terminal.get("connected", False):
                raise Mt5Unavailable("Terminal is not connected to the broker server")
        except (LiveTradingForbidden, Mt5Unavailable):
            self.mt5.shutdown()
            raise
        self.connected = True
        return self.account_summary(account, terminal)

    def shutdown(self) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()
        self.connected = False

    def _require(self) -> ReadOnlyMt5:
        if not self.connected or self.mt5 is None:
            raise Mt5Unavailable("Not connected")
        terminal = _as_dict(self.mt5.terminal_info())
        if not terminal.get("connected", False):
            self.connected = False
            raise Mt5Unavailable("Terminal disconnected from broker server")
        return self.mt5

    @staticmethod
    def account_summary(account: dict, terminal: dict) -> dict:
        """Non-sensitive account facts (no password; login kept for reconciliation only)."""
        return {"login": account.get("login"), "server": account.get("server"), "company": account.get("company"),
                "currency": account.get("currency"), "trade_mode": account.get("trade_mode"),
                "is_demo": account.get("trade_mode") == 0, "leverage": account.get("leverage"),
                "equity": account.get("equity"), "balance": account.get("balance"),
                "margin_free": account.get("margin_free"), "trade_allowed": account.get("trade_allowed"),
                "terminal_build": terminal.get("build"), "terminal_connected": terminal.get("connected"),
                "terminal_trade_allowed": terminal.get("trade_allowed")}

    # -- symbol discovery ----------------------------------------------------------------------
    def discover_xauusd(self) -> str:
        """The broker's XAUUSD symbol: exact 'XAUUSD', else a unique XAUUSD-prefixed name, else
        a unique symbol with base XAU and profit USD. Ambiguity fails closed."""
        mt5 = self._require()
        symbols = [_as_dict(s) for s in (mt5.symbols_get() or ())]
        names = [s.get("name", "") for s in symbols]
        if "XAUUSD" in names:
            return "XAUUSD"
        prefixed = [n for n in names if _XAU_NAME.match(n)]
        if len(prefixed) == 1:
            return prefixed[0]
        by_currency = [s["name"] for s in symbols
                       if s.get("currency_base") == "XAU" and s.get("currency_profit") == "USD"]
        if len(prefixed) == 0 and len(by_currency) == 1:
            return by_currency[0]
        raise ValidationError(f"Cannot uniquely identify XAUUSD (candidates: {prefixed or by_currency})")

    def symbol_specification(self, name: str) -> SpecDiscovery:
        mt5 = self._require()
        mt5.symbol_select(name, True)
        info = _as_dict(mt5.symbol_info(name))
        account = _as_dict(mt5.account_info())
        tick = _as_dict(mt5.symbol_info_tick(name))
        now = datetime.now(timezone.utc).isoformat()
        missing, problems = [], []
        if not info:
            return SpecDiscovery(name, now, {}, ["symbol_info"], [])

        def take(key, source=info):
            value = source.get(key)
            if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0
                                 and key not in ("trade_stops_level", "trade_freeze_level", "spread")):
                missing.append(key)
            return value

        margin = None
        ask = tick.get("ask")
        if ask:
            margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, name, 1.0, ask)
        if not margin:
            missing.append("margin_per_lot")
        # Cross-check the reported loss tick value against the terminal's own P&L engine
        # (order_calc_profit) and contract_size x tick_size. Brokers can misreport tick values
        # (e.g. CFD calc modes); sizing must use the value matching actual P&L. If sources disagree,
        # use the LARGEST loss per tick (smallest position = conservative) and record the discrepancy.
        tick_check = {}
        tick_size, reported_loss = info.get("trade_tick_size"), info.get("trade_tick_value_loss")
        if ask and tick_size and reported_loss:
            ticks = 1000  # measure over a larger move to avoid float noise, then per tick
            calc = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, name, 1.0, ask, ask - ticks * tick_size)
            implied = (info.get("trade_contract_size") or 0) * tick_size
            candidates = {"reported_tick_value_loss": round(float(reported_loss), 8)}
            if calc is not None:
                candidates["order_calc_profit"] = round(abs(float(calc)) / ticks, 8)
            if implied:
                candidates["contract_size_x_tick_size"] = round(float(implied), 8)
            used = max(candidates.values())
            agree = max(candidates.values()) - min(candidates.values()) <= 0.01 * used
            tick_check = {"sources": candidates, "consistent": agree, "used": used}
            if calc is None:
                missing.append("order_calc_profit cross-check")
        trade_mode = info.get("trade_mode")
        fields = {
            "broker_symbol": name,
            "description": info.get("description"),
            "path": info.get("path"),
            "contract_size": take("trade_contract_size"),
            "tick_size": take("trade_tick_size"),
            "tick_value": take("trade_tick_value"),
            "tick_value_loss": take("trade_tick_value_loss"),
            "point": take("point"),
            "digits": info.get("digits"),
            "volume_min": take("volume_min"),
            "volume_max": take("volume_max"),
            "volume_step": take("volume_step"),
            "currency_base": info.get("currency_base"),
            "currency_profit": take("currency_profit"),
            "currency_margin": take("currency_margin"),
            "account_currency": account.get("currency"),
            "order_types": [n for bit, n in ORDER_MODE_FLAGS.items() if (info.get("order_mode") or 0) & bit],
            "filling_modes": [n for bit, n in FILLING_FLAGS.items() if (info.get("filling_mode") or 0) & bit],
            "stops_level_points": take("trade_stops_level"),
            "freeze_level_points": info.get("trade_freeze_level"),
            "current_spread_points": info.get("spread"),
            "spread_float": info.get("spread_float"),
            "trade_mode": SYMBOL_TRADE_MODES.get(trade_mode, f"unknown:{trade_mode}"),
            "margin_per_lot": margin,
            "commission_per_lot_round_trip": self._commission,
            "trade_calc_mode": info.get("trade_calc_mode"),
            "tick_value_check": tick_check,
            "tick_value_loss_used": tick_check.get("used", info.get("trade_tick_value_loss")),
            "server": account.get("server"),
            "company": account.get("company"),
        }
        if fields["account_currency"] is None:
            missing.append("account_currency")
        if self._commission is None:
            missing.append("commission_per_lot_round_trip (not exposed by MT5; confirm with broker)")
        if trade_mode != 4:
            problems.append(f"Symbol trade mode is {fields['trade_mode']}, not full")
        if "sl" not in fields["order_types"]:
            problems.append("Broker does not accept stop-loss on orders for this symbol")
        if fields["currency_profit"] and fields["account_currency"] and \
                fields["currency_profit"] != fields["account_currency"]:
            problems.append("Profit currency differs from account currency (tick value conversion must be "
                            "verified)")
        return SpecDiscovery(name, now, fields, sorted(set(missing)), problems)

    # -- time verification ------------------------------------------------------------------------
    def verify_server_time(self, name: str, *, now: datetime | None = None) -> dict:
        """Verify the configured server offset against a fresh tick (requires an open market)."""
        mt5 = self._require()
        tick = _as_dict(mt5.symbol_info_tick(name))
        if not tick.get("time_msc"):
            raise ServerClockError("No tick available to verify server time")
        now = now or datetime.now(timezone.utc)
        offset = verify_server_offset(self.server_tz, tick_server_msc=int(tick["time_msc"]), utc_now=now)
        return {"method": "fresh_tick", "verified_at": now.isoformat(), "offset_hours": offset / timedelta(hours=1),
                "tz": self.server_tz.name}

    # -- market data --------------------------------------------------------------------------------
    def quote(self, name: str, *, now: datetime | None = None, max_age: timedelta = timedelta(seconds=30)) -> Quote:
        mt5 = self._require()
        tick = _as_dict(mt5.symbol_info_tick(name))
        if not tick or not tick.get("bid") or not tick.get("ask"):
            raise Mt5Unavailable(f"No quote for {name}")
        moment = self.server_tz.server_msc_to_utc(int(tick["time_msc"]))
        now = now or datetime.now(timezone.utc)
        if now - moment > max_age:
            raise ValidationError(f"Stale quote: last tick {now - moment} ago (market closed or feed stalled)")
        return Quote.create(symbol="XAUUSD", bid=tick["bid"], ask=tick["ask"], time=moment)

    def history(self, name: str, interval: str, start: datetime, end: datetime, *,
                chunk_days: int = 30) -> list[Mt5Bar]:
        """Closed bars in [start, end) UTC, converted from server time. Any ambiguous or
        unconvertible server timestamp rejects the whole request (fail closed)."""
        mt5 = self._require()
        if interval not in TIMEFRAMES:
            raise ValueError(f"Unsupported interval {interval}")
        mt5.symbol_select(name, True)  # history requests fail with "Invalid params" for unselected symbols
        # MT5 interprets request bounds on the same (server wall time) scale as bar times. Request in
        # chunks so a long range never exceeds the terminal's max-bars limit.
        rows, seen, cursor = [], set(), start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            server_start = self.server_tz.to_server(cursor).replace(tzinfo=timezone.utc)
            server_end = self.server_tz.to_server(chunk_end).replace(tzinfo=timezone.utc)
            rates = mt5.copy_rates_range(name, getattr(mt5, TIMEFRAMES[interval]), server_start, server_end)
            if rates is None:
                raise Mt5Unavailable(f"copy_rates_range failed: {mt5.last_error()}")
            for row in rates:
                if int(row["time"]) not in seen:
                    seen.add(int(row["time"]))
                    rows.append(row)
            cursor = chunk_end
        rows.sort(key=lambda r: int(r["time"]))
        out = []
        for row in rows:
            record = {k: row[k] for k in ("time", "open", "high", "low", "close", "tick_volume", "spread")}
            utc = self.server_tz.server_epoch_to_utc(int(record["time"]))
            bar = Bar.create(symbol="XAUUSD", interval=interval, open_time=utc, open=float(record["open"]),
                             high=float(record["high"]), low=float(record["low"]), close=float(record["close"]),
                             volume=int(record["tick_volume"]))
            if bar.close_time <= end:
                out.append(Mt5Bar(bar, int(record["time"]), int(record["spread"]), int(record["tick_volume"])))
        return out


def _tick_rows(mt5, name: str, server_start: datetime, server_end: datetime):
    flags = getattr(mt5, "COPY_TICKS_INFO", 1)  # bid/ask changes
    return mt5.copy_ticks_range(name, server_start, server_end, flags)


def read_ticks(conn: "Mt5ReadOnlyConnector", name: str, start: datetime, end: datetime,
               *, chunk: timedelta = timedelta(hours=6)) -> list[tuple[datetime, float, float]]:
    """Read-only bid/ask ticks in [start, end) UTC as (utc_time, bid, ask), converted from server time."""
    mt5 = conn._require()
    mt5.symbol_select(name, True)
    out, cursor = [], start
    while cursor < end:
        nxt = min(cursor + chunk, end)
        rows = _tick_rows(mt5, name, conn.server_tz.to_server(cursor).replace(tzinfo=timezone.utc),
                          conn.server_tz.to_server(nxt).replace(tzinfo=timezone.utc))
        if rows is None:
            raise Mt5Unavailable(f"copy_ticks_range failed: {mt5.last_error()}")
        for row in rows:
            bid, ask = float(row["bid"]), float(row["ask"])
            if bid > 0 and ask >= bid:
                out.append((conn.server_tz.server_msc_to_utc(int(row["time_msc"])), bid, ask))
        cursor = nxt
    return out


def commission_evidence(conn: "Mt5ReadOnlyConnector", name: str) -> dict:
    """Read-only: commission actually charged on this account's own deals in `name`.

    MT5 exposes no per-symbol commission schedule. Commission counts as verified only when the account
    history contains executed deals in the symbol (deal `commission` per deal volume, per side). An empty
    history leaves it unverified; no trade is ever placed to obtain it."""
    mt5 = conn._require()
    start, end = datetime(2000, 1, 1), datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2)
    rows = [_as_dict(d) for d in (mt5.history_deals_get(start, end) or ())]
    symbol_deals = [d for d in rows if d.get("symbol") == name and (d.get("volume") or 0) > 0]
    per_lot = sorted({round(abs(float(d.get("commission") or 0.0)) / float(d["volume"]), 4) for d in symbol_deals})
    result = {"source": "mt5.history_deals_get (read-only, this account)", "account_deals": len(rows),
              "symbol_deals": len(symbol_deals), "commission_per_lot_per_side_observed": per_lot,
              "fees_observed": sorted({float(d.get("fee") or 0.0) for d in symbol_deals})}
    if not symbol_deals:
        result.update(verified=False, status=f"unverified: no executed {name} deals in this account's history")
    elif len(per_lot) == 1:
        result.update(verified=True, status=f"verified from {len(symbol_deals)} deals: {per_lot[0]} per lot per side "
                                            f"({round(2 * per_lot[0], 4)} round trip)")
    else:
        result.update(verified=False, status="inconsistent commission across deals; confirm with the broker")
    return result


def spread_statistics(bars: list[Mt5Bar], point: Decimal) -> dict:
    """Observed spread distribution from MT5 bar spread fields (points -> price units)."""
    if not bars:
        raise ValidationError("No bars")
    points = sorted(b.spread_points for b in bars)
    pick = lambda q: points[min(len(points) - 1, int(q * len(points)))]  # noqa: E731
    return {"bars": len(points), "median": str(point * pick(0.5)), "p90": str(point * pick(0.9)),
            "p99": str(point * pick(0.99)), "max": str(point * points[-1]),
            "mean": str(point * Decimal(str(statistics.fmean(points))))}


def spread_model(bars: list[Mt5Bar], point: Decimal, *, floor: Decimal | None = None):
    """Per-bar spread for replay: the bar's recorded spread (MT5 reports the minimum spread
    of the bar, so `floor` lets research impose a conservative minimum)."""
    by_time = {b.bar.open_time: point * b.spread_points for b in bars}

    def model(bar: Bar) -> Decimal:
        value = by_time.get(bar.open_time)
        if value is None:
            raise ValidationError(f"No spread recorded for bar {bar.open_time.isoformat()}")
        return max(value, floor) if floor is not None else value

    return model
