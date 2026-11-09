"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { usePoll } from "@/lib/api";
import { ageFrom, dash, fmtAge, fmtMoney, fmtNum, fmtPct, fmtSigned, fmtTime, tone } from "@/lib/format";
import type { Account, Health, MarketStatus, Overview, RiskStatus, StrategyPanel } from "@/lib/types";
import { Empty, ErrorNote, KindTag, Loading, Panel, Stat, StateBadge } from "@/components/ui";

function MarketCard({ m, h }: { m: MarketStatus; h?: Health }) {
  if (!m.available) {
    return <Panel title={m.label}><Empty>Runner not started. No observations recorded.</Empty></Panel>;
  }
  const q = m.last_quote;
  const mid = q ? (q.bid + q.ask) / 2 : null;
  const digits = 2;
  const dataAge = ageFrom(m.last_market_data_at);
  const open = m.market_open;
  return (
    <Panel
      title={<span className="flex items-center gap-2">{m.label} <span className="normal-case tracking-normal text-mute">{m.venue}</span></span>}
      right={<StateBadge state={m.runner} label={`runner ${m.runner}`} />}>
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Stat label="Last quote (mid)" kind={q ? "observed" : "unavailable"} value={q ? fmtNum(mid, digits) : dash}
          sub={q ? <>bid {fmtNum(q.bid, digits)} · ask {fmtNum(q.ask, digits)}<br />{fmtAge(q.age_s)} old</> : "no quote recorded"} />
        <Stat label="Market" value={<StateBadge state={open === false ? "closed" : open ? "ok" : "unknown"}
          label={open === false ? "closed" : open ? "open" : "unknown"} />}
          sub={m.calendar ? `calendar: ${m.calendar}` : undefined} />
        <Stat label="Data" value={<StateBadge state={m.data === "fresh" || m.data === "ok" ? "fresh" : m.data ? "stale" : "unknown"} label={m.data ?? "unknown"} />}
          sub={`last update ${dataAge === null ? dash : fmtAge(dataAge) + " ago"}`} />
        <Stat label="New entries" value={<StateBadge state={m.new_entries === "allowed" ? "allowed" : m.new_entries ? "blocked" : "unknown"} label={m.new_entries ?? "unknown"} />}
          sub={m.entry_block_reasons?.length ? m.entry_block_reasons.join(", ") : "guards pass"} />
      </div>
      <dl className="mt-4 grid grid-cols-1 gap-x-8 border-t border-line pt-3 sm:grid-cols-2">
        <div className="kv"><dt>Connection</dt><dd><StateBadge state={m.connection === "connected" ? "connected" : "disconnected"} label={m.connection ?? dash} /></dd></div>
        <div className="kv"><dt>Last heartbeat</dt><dd>{fmtTime(m.last_heartbeat, false)} · {fmtAge(m.last_heartbeat_age_s)} ago</dd></div>
        <div className="kv"><dt>Last market-data update</dt><dd>{fmtTime(m.last_market_data_at)}</dd></div>
        <div className="kv"><dt>Last recorded bar (open)</dt><dd>{fmtTime(m.last_bar_open)}</dd></div>
        <div className="kv"><dt>Uptime (window)</dt><dd>{h?.uptime ? `${fmtPct(h.uptime.runner_uptime_pct)} · ${h.uptime.heartbeats} beats` : dash}</dd></div>
        <div className="kv"><dt>Fresh data while open</dt><dd>{h?.uptime?.fresh_data_pct_while_open == null ? dash : fmtPct(h.uptime.fresh_data_pct_while_open)}</dd></div>
        {m.websocket_reconnects !== undefined && <div className="kv"><dt>WebSocket reconnects (since start)</dt><dd>{m.websocket_reconnects ?? dash}</dd></div>}
        {m.connection_failures !== undefined && <div className="kv"><dt>Connection failures (since start)</dt><dd>{m.connection_failures ?? dash}</dd></div>}
        <div className="kv"><dt>Pre-registration</dt><dd>{h?.integrity ? (h.integrity.preregistration_consistent ? <span className="text-up">consistent</span> : <span className="text-down">MISMATCH</span>) : dash}</dd></div>
        <div className="kv"><dt>Forward bars recorded</dt><dd>{h?.integrity?.live_bars ?? dash}</dd></div>
      </dl>
      {m.demo_account ? (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg bg-ink-850 px-3 py-2 text-xs">
          <KindTag kind="observed" />
          <span className="text-mute">MT5 demo account (never traded)</span>
          <span className="num">balance {fmtMoney(m.demo_account.balance)}</span>
          <span className="num">equity {fmtMoney(m.demo_account.equity)}</span>
          <span className="num">positions {m.demo_account.positions} · orders {m.demo_account.orders}</span>
          <span className="text-mute">checked {fmtTime(m.demo_account.checked_at, false)}</span>
        </div>
      ) : m.market === "btcusdt" ? (
        <div className="mt-3 rounded-lg bg-ink-850 px-3 py-2 text-xs text-mute">
          <KindTag kind="unavailable" /> No exchange account is connected: Binance public market data only.
        </div>
      ) : null}
      {m.last_error && <div className="mt-2 truncate text-[11px] text-mute" title={m.last_error}>Last connector error (historical): {m.last_error}</div>}
    </Panel>
  );
}

function RiskCard({ name, r }: { name: string; r: RiskStatus }) {
  const L = r.limits;
  return (
    <div className="rounded-lg border border-line bg-ink-850 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-semibold text-slate-200">{name === "xauusd" ? "XAUUSD" : "BTC/USDT"}</span>
        <StateBadge state={r.state} />
      </div>
      <dl className="text-[12.5px]">
        <div className="kv"><dt>Risk per trade (max)</dt><dd>{fmtPct(L.risk_per_trade_pct, 1)}</dd></div>
        <div className="kv"><dt>Daily loss halt</dt><dd>{fmtPct(L.daily_loss_halt_pct, 1)}</dd></div>
        <div className="kv"><dt>Aggregate open risk (max)</dt><dd>{fmtPct(L.aggregate_open_risk_pct, 1)}</dd></div>
        <div className="kv"><dt>Open risk now (worst book)</dt><dd>{fmtPct(r.max_open_risk_pct_of_equity, 3)}</dd></div>
        <div className="kv"><dt>Accounts halted today</dt><dd>{r.halted_accounts_today.length}</dd></div>
        <div className="kv"><dt>KILL · STOP · breaker</dt>
          <dd>{r.controls.kill_file ? <span className="text-down">KILL</span> : "–"} · {r.controls.stop_file ? <span className="text-warn">STOP</span> : "–"} · {r.controls.breaker_trips.length ? <span className="text-down">{r.controls.breaker_trips.length} trip(s)</span> : "clear"}</dd></div>
      </dl>
    </div>
  );
}

function StrategySummary({ s }: { s: StrategyPanel }) {
  const m = s.metrics;
  return (
    <Link href={`/strategies/#${s.id}`} className="block rounded-lg border border-line bg-ink-850 p-3 transition hover:border-ink-600">
      <div className="flex items-start justify-between gap-2">
        <div className="text-[13px] font-semibold leading-snug text-slate-100">{s.name}</div>
        <StateBadge state={s.lifecycle} />
      </div>
      {m ? (
        <div className="mt-3 grid grid-cols-3 gap-3">
          <Stat label="Net P&L" value={fmtMoney(m.net_pnl)} valueClass={tone(m.net_pnl)} />
          <Stat label="Today" value={fmtSigned(m.pnl.today)} valueClass={tone(m.pnl.today)} />
          <Stat label="Trades" value={m.trades} sub={`${s.signals?.forward ?? 0} forward signals`} />
        </div>
      ) : (
        <div className="mt-3 text-xs leading-relaxed text-mute">{s.lifecycle_detail}</div>
      )}
      <div className="mt-2 text-[11px] text-mute">Last activity {fmtTime(s.last_activity)}</div>
    </Link>
  );
}

function AccountsTable({ accounts }: { accounts: Account[] }) {
  const [all, setAll] = useState(false);
  const rows = useMemo(() => (all ? accounts : accounts.filter((a) => !a.strategy.startsWith("random"))), [accounts, all]);
  return (
    <Panel title={<span className="flex items-center gap-2">Simulated accounts <KindTag kind="simulated" /></span>}
      right={<button className={`btn ${all ? "btn-on" : ""}`} onClick={() => setAll(!all)}>{all ? "All books" : "Candidate strategies"}</button>}>
      <p className="mb-2 text-[11.5px] text-mute">
        Each book replays the same signals under one equity size and one cost scenario. Books are alternative simulations, so they are never summed.
      </p>
      <div className="max-h-[340px] overflow-auto">
        <table className="tbl">
          <thead><tr><th>Market</th><th>Book</th><th>Cost</th><th className="text-right">Trades</th><th className="text-right">Realized</th><th className="text-right">Equity</th><th className="text-right">Return</th></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.market + a.book}>
                <td className="text-mute">{a.market === "xauusd" ? "XAU" : "BTC"}</td>
                <td className="num">{a.book}</td>
                <td className="num text-mute">{a.cost_scenario}</td>
                <td className="num text-right">{a.trades}</td>
                <td className={`num text-right ${tone(a.realized)}`}>{fmtNum(a.realized)}</td>
                <td className="num text-right">{fmtNum(a.equity)}</td>
                <td className={`num text-right ${tone(a.return_pct)}`}>{fmtSigned(a.return_pct, 2, "%")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export default function OverviewPage() {
  const { data, error, loading, updatedAt } = usePoll<Overview>("/api/overview", 15000);
  if (loading && !data) return <Loading />;
  if (!data) return <ErrorNote error={error} />;
  const markets = Object.values(data.markets);
  const disconnections = Object.entries(data.health).flatMap(([mk, h]) =>
    Object.entries(h.disconnections ?? {}).map(([k, v]) => ({ mk, k, v })));
  const gaps = Object.entries(data.health).flatMap(([mk, h]) => (h.uptime?.gaps_over_3_min ?? []).map((g) => ({ mk, ...g })));
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold text-slate-100">Overview</h1>
        <div className="flex items-center gap-3 text-[11px] text-mute">
          <span className="flex items-center gap-1"><KindTag kind="observed" /> venue data</span>
          <span className="flex items-center gap-1"><KindTag kind="simulated" /> shadow books</span>
          <span className="flex items-center gap-1"><KindTag kind="derived" /> computed</span>
          <span className="flex items-center gap-1"><KindTag kind="unavailable" /> not recorded</span>
          <span>· refreshed {updatedAt ? fmtTime(updatedAt.toISOString(), false) : dash}</span>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {markets.map((m) => <MarketCard key={m.market} m={m} h={data.health[m.market]} />)}
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title={<span className="flex items-center gap-2">Strategies <KindTag kind="simulated" /></span>}
          right={<Link href="/strategies/" className="btn">Details</Link>} className="xl:col-span-2">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {data.strategies.map((s) => <StrategySummary key={s.id} s={s} />)}
          </div>
        </Panel>
        <Panel title="Risk limits">
          <div className="grid grid-cols-1 gap-3">
            {Object.entries(data.risk).map(([k, r]) => <RiskCard key={k} name={k} r={r} />)}
          </div>
        </Panel>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2"><AccountsTable accounts={data.accounts} /></div>
        <Panel title={`Alerts & interruptions (${data.alerts.length})`}>
          {data.alerts.length === 0 && disconnections.length === 0 && gaps.length === 0 ? (
            <div className="text-sm text-up">No alerts, interruptions or heartbeat gaps in the health window.</div>
          ) : (
            <ul className="space-y-1.5 text-[12.5px]">
              {data.alerts.map((a, i) => (
                <li key={i} className="flex gap-2"><span className="w-10 shrink-0 text-mute">{a.market === "xauusd" ? "XAU" : "BTC"}</span><span className="text-warn">{a.text}</span></li>
              ))}
              {disconnections.map((d, i) => (
                <li key={`d${i}`} className="flex gap-2"><span className="w-10 shrink-0 text-mute">{d.mk === "xauusd" ? "XAU" : "BTC"}</span><span>{d.k.replace(/_/g, " ")}: <span className="num">{d.v}</span></span></li>
              ))}
              {gaps.map((g, i) => (
                <li key={`g${i}`} className="flex gap-2"><span className="w-10 shrink-0 text-mute">{g.mk === "xauusd" ? "XAU" : "BTC"}</span><span>heartbeat gap {g.minutes} min from {fmtTime(g.from)}</span></li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
