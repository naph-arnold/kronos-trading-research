"use client";

import dynamic from "next/dynamic";
import { Fragment, useState } from "react";
import { usePoll } from "@/lib/api";
import { dash, fmtAge, fmtMoney, fmtNum, fmtPct, fmtShort, fmtSigned, tone } from "@/lib/format";
import { Empty, ErrorNote, KindTag, Loading, Panel, Stat, StateBadge } from "@/components/ui";

const EquityChart = dynamic(() => import("@/components/EquityChart"), { ssr: false });

type Perf = {
  closed_trades: number; net_profit: number; gross_before_costs: number; win_rate_pct: number | null; profit_factor: number | null;
  average_net: number | null; mean_r: number | null; longest_losing_streak: number | null; max_drawdown: number | null;
  max_drawdown_pct: number | null; today: number; this_week: number; this_month: number;
  costs: Record<string, number>; curve: { time: number; value: number }[];
  by_strategy: Record<string, { trades: number; net: number; win_rate_pct: number }>;
  by_session: Record<string, { trades: number; net: number; win_rate_pct: number }>;
  by_timeframe: Record<string, { trades: number; net: number; win_rate_pct: number }>;
};
type Trade = { id: string; strategy: string | null; side: string | null; opened_at: string | null; closed_at: string; volume: number | null;
  entry: number | null; exit: number | null; stop: number | null; target: number | null; gross: number; commission: number; swap: number;
  fee: number; spread_slippage: number; net: number; planned_risk: number | null; r: number | null; session: string | null;
  timeframe: string | null; exit_reason: string | null; reason: unknown };
type View = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const MODES = [
  { id: "broker", label: "MT5 demo broker", kind: "observed" as const },
  { id: "shadow", label: "Shadow forward test", kind: "simulated" as const },
  { id: "historical", label: "Historical research", kind: "derived" as const },
  { id: "live", label: "Real money (future)", kind: "unavailable" as const },
];

function Breakdown({ title, rows }: { title: string; rows: Perf["by_session"] }) {
  const e = Object.entries(rows ?? {});
  return (
    <div>
      <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-mute">{title}</div>
      {e.length === 0 ? <p className="text-[12px] text-mute">No closed trades.</p> : (
        <table className="tbl"><tbody>{e.map(([k, v]) => (
          <tr key={k}><td>{k}</td><td className="num text-right">{v.trades}</td><td className={`num text-right ${tone(v.net)}`}>{fmtSigned(v.net)}</td><td className="num text-right text-mute">{fmtPct(v.win_rate_pct, 0)}</td></tr>))}</tbody></table>
      )}
    </div>
  );
}

function PerformancePanel({ p, currency = "USD", capitalLabel }: { p: Perf; currency?: string; capitalLabel: string }) {
  const c = p.costs ?? {};
  return (
    <>
      <div className="grid grid-cols-2 gap-4 rounded-2xl border border-line bg-ink-900/80 p-4 sm:grid-cols-4 xl:grid-cols-8">
        <Stat label="Net profit (all closed)" value={fmtMoney(p.net_profit)} valueClass={tone(p.net_profit)} sub={`${p.closed_trades} closed trades`} />
        <Stat label="Today" value={fmtSigned(p.today)} valueClass={tone(p.today)} sub="realized, UTC day" />
        <Stat label="This week" value={fmtSigned(p.this_week)} valueClass={tone(p.this_week)} />
        <Stat label="This month" value={fmtSigned(p.this_month)} valueClass={tone(p.this_month)} />
        <Stat label="Win rate" value={p.win_rate_pct == null ? dash : fmtPct(p.win_rate_pct, 1)} sub={p.mean_r == null ? undefined : `mean ${fmtNum(p.mean_r, 3)} R`} />
        <Stat label="Profit factor" value={p.profit_factor == null ? dash : fmtNum(p.profit_factor, 2)} />
        <Stat label="Max drawdown" value={p.max_drawdown == null ? dash : fmtMoney(p.max_drawdown)} sub={p.max_drawdown_pct == null ? undefined : `${fmtNum(p.max_drawdown_pct, 2)}% (closed trades)`} />
        <Stat label="Longest losing streak" value={p.longest_losing_streak ?? dash} />
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title={`Trading-equity curve (${capitalLabel} + closed-trade P&L; ${currency})`} className="xl:col-span-2">
          {p.curve.length === 0 ? <Empty>No closed trades yet: nothing to draw, and nothing is invented.</Empty> : (
            <EquityChart books={[{ book: "net", strategy: "", initial_equity: 0, commission: 0, points: p.curve.length, trades: p.curve.length,
              marks: 0, realized_pnl: 0, realized: [], equity: p.curve, drawdown_pct: [], last: p.curve[p.curve.length - 1].value, max_drawdown_pct: 0 }]} height={260} />
          )}
          <p className="mt-2 text-[11px] text-mute">Deposits and withdrawals are cash flows and are excluded, so they never appear as profit or drawdown.</p>
        </Panel>
        <Panel title="Costs (negative = cost)">
          <dl className="text-[12.5px]">
            <div className="kv"><dt>Gross before costs</dt><dd className={tone(p.gross_before_costs)}>{fmtSigned(p.gross_before_costs)}</dd></div>
            <div className="kv"><dt>Spread + slippage</dt><dd>{fmtSigned(c.spread_slippage)}</dd></div>
            <div className="kv"><dt>Commission</dt><dd>{fmtSigned(c.commission)}</dd></div>
            <div className="kv"><dt>Swap / financing</dt><dd>{fmtSigned(c.swap)}</dd></div>
            <div className="kv"><dt>Fees</dt><dd>{fmtSigned(c.fee)}</dd></div>
            <div className="kv"><dt>Account charge deals</dt><dd>{fmtSigned(c.account_cost_deals)}</dd></div>
            <div className="kv"><dt><b>Net</b></dt><dd className={`font-semibold ${tone(p.net_profit)}`}>{fmtSigned(p.net_profit)}</dd></div>
          </dl>
          <p className="mt-2 text-[11px] text-mute">Broker fills already contain the spread, so broker spread appears inside gross; simulated books itemize it.</p>
        </Panel>
      </div>
      <Panel title="Performance by strategy, session and timeframe">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Breakdown title="Strategy" rows={p.by_strategy} /><Breakdown title="Session (UTC entry)" rows={p.by_session} /><Breakdown title="Timeframe" rows={p.by_timeframe} />
        </div>
      </Panel>
    </>
  );
}

function TradesTable({ trades, empty }: { trades: Trade[]; empty: string }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!trades?.length) return <Empty>{empty}</Empty>;
  return (
    <div className="max-h-[520px] overflow-auto rounded-lg border border-line/70">
      <table className="tbl">
        <thead><tr><th>Closed</th><th>Strategy</th><th>Side</th><th className="text-right">Entry → exit</th><th className="text-right">Gross</th><th className="text-right">Costs</th><th className="text-right">Net</th><th className="text-right">R</th><th>Exit</th></tr></thead>
        <tbody>{trades.map((t) => {
          const costs = t.commission + t.swap + t.fee + t.spread_slippage;
          return (
            <Fragment key={t.id}>
              <tr className="cursor-pointer" onClick={() => setOpen(open === t.id ? null : t.id)} aria-expanded={open === t.id}>
                <td className="num whitespace-nowrap">{fmtShort(t.closed_at)}</td><td>{t.strategy}</td>
                <td className={t.side === "long" ? "text-up" : "text-down"}>{t.side}</td>
                <td className="num text-right">{fmtNum(t.entry)} → {fmtNum(t.exit)}</td>
                <td className={`num text-right ${tone(t.gross)}`}>{fmtSigned(t.gross)}</td><td className="num text-right">{fmtSigned(costs)}</td>
                <td className={`num text-right font-semibold ${tone(t.net)}`}>{fmtSigned(t.net)}</td>
                <td className="num text-right">{t.r == null ? dash : fmtSigned(t.r)}</td><td>{t.exit_reason ?? dash}</td>
              </tr>
              {open === t.id && (
                <tr><td colSpan={9} className="bg-ink-950/60">
                  <dl className="grid grid-cols-2 gap-x-6 text-[12px] md:grid-cols-4">
                    <div className="kv"><dt>Opened</dt><dd>{fmtShort(t.opened_at)}</dd></div><div className="kv"><dt>Volume</dt><dd>{fmtNum(t.volume, 3)}</dd></div>
                    <div className="kv"><dt>Stop</dt><dd>{fmtNum(t.stop)}</dd></div><div className="kv"><dt>Target</dt><dd>{fmtNum(t.target)}</dd></div>
                    <div className="kv"><dt>Planned risk</dt><dd>{fmtMoney(t.planned_risk)}</dd></div><div className="kv"><dt>Spread + slippage</dt><dd>{fmtSigned(t.spread_slippage)}</dd></div>
                    <div className="kv"><dt>Commission</dt><dd>{fmtSigned(t.commission)}</dd></div><div className="kv"><dt>Swap</dt><dd>{fmtSigned(t.swap)}</dd></div>
                    <div className="kv"><dt>Session · timeframe</dt><dd>{t.session ?? dash} · {t.timeframe ?? dash}</dd></div><div className="kv"><dt>Trade id</dt><dd className="truncate">{t.id}</dd></div>
                  </dl>
                  {t.reason != null && <div className="mt-2 text-[11.5px] text-mute">Why taken (strategy context at the signal): <span className="num text-slate-300">{JSON.stringify(t.reason)}</span></div>}
                </td></tr>
              )}
            </Fragment>
          );
        })}</tbody>
      </table>
    </div>
  );
}

function Broker({ v }: { v: View }) {
  const a = v.account;
  const conn = v.connection as string;
  return (
    <div className="space-y-4">
      <div className={`rounded-xl border px-3 py-2 text-[12.5px] ${conn === "connected" ? "border-info/30 bg-info/10 text-info" : "border-down/40 bg-down/10 text-down"}`}>
        <b>MT5 DEMO account</b> · demo funds are not withdrawable money · broker-confirmed figures only ·{" "}
        snapshot {v.last_snapshot_at ? `${fmtShort(v.last_snapshot_at)} (${fmtAge(v.last_snapshot_age_s)} ago)` : "none"} · connection <b>{conn}</b>
        {v.note && <div className="mt-1">{v.note}</div>}
      </div>
      {!a ? <Empty>Broker account data unavailable. Balances are not shown as zero.</Empty> : (
        <div className="grid grid-cols-2 gap-4 rounded-2xl border border-line bg-ink-900/80 p-4 sm:grid-cols-4 xl:grid-cols-8">
          <Stat label="Balance" kind="observed" value={fmtMoney(a.balance)} sub={a.currency} />
          <Stat label="Equity" kind="observed" value={fmtMoney(a.equity)} />
          <Stat label="Margin used" value={fmtMoney(a.margin)} />
          <Stat label="Free margin" value={fmtMoney(a.margin_free)} sub={a.margin_level ? `level ${fmtNum(a.margin_level, 0)}%` : "no open margin"} />
          <Stat label="Unrealized P&L" value={fmtSigned(v.open_positions?.floating_total)} valueClass={tone(v.open_positions?.floating_total)} sub={`${v.open_positions?.positions?.length ?? 0} open positions`} />
          <Stat label="Deposits" value={fmtMoney(v.cash_flows?.deposits)} sub="cash flow, not profit" />
          <Stat label="Withdrawals" value={fmtMoney(v.cash_flows?.withdrawals)} sub="cash flow" />
          <Stat label="Account" value={<span className="text-sm">{a.login}</span>} sub={`${a.server} · 1:${a.leverage}`} />
        </div>
      )}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title="Connection & reconciliation">
          <dl className="text-[12.5px]">
            <div className="kv"><dt>Terminal connected</dt><dd>{v.terminal ? String(v.terminal.connected) : dash}</dd></div>
            <div className="kv"><dt>Algo Trading enabled in terminal</dt><dd className={v.terminal?.algo_trading_enabled ? "text-up" : "text-warn"}>{v.terminal ? String(v.terminal.algo_trading_enabled) : dash}</dd></div>
            <div className="kv"><dt>Last successful snapshot</dt><dd>{fmtShort(v.last_successful_at)}</dd></div>
            <div className="kv"><dt>Balance reconciles with deals</dt><dd className={v.reconciliation?.balance_check?.ok ? "text-up" : "text-down"}>{v.reconciliation ? (v.reconciliation.balance_check.ok ? "yes" : `difference ${fmtNum(v.reconciliation.balance_check.difference)}`) : dash}</dd></div>
            <div className="kv"><dt>Broker deals recorded</dt><dd>{v.reconciliation?.deals ?? dash}</dd></div>
          </dl>
          <div className="mt-3 text-[10.5px] font-semibold uppercase tracking-wider text-mute">Runners&apos; read-only account checks</div>
          <ul className="mt-1 space-y-0.5 text-[12px]">{(v.runner_checks ?? []).map((r: View) => (
            <li key={r.runner}>{r.runner}: {fmtMoney(r.balance)} / {fmtMoney(r.equity)} · {r.positions} pos · {r.orders} orders · {fmtShort(r.at)}</li>))}</ul>
        </Panel>
        <Panel title="Execution log (demo orders)" className="xl:col-span-2">
          <div className="mb-2 flex flex-wrap gap-2 text-[12px]">
            <StateBadge state={v.execution?.authorized ? "running" : "not activated"} label={v.execution?.authorized ? "trial authorized" : "execution trial NOT authorized"} />
            {Object.entries(v.execution?.counts ?? {}).map(([k, n]) => <span key={k} className="rounded-full border border-line px-2 py-0.5">{k} <b className="num">{String(n)}</b></span>)}
          </div>
          {!(v.execution?.orders?.length) ? <Empty>{v.execution?.note ?? "No orders recorded."}</Empty> : (
            <div className="max-h-72 overflow-auto"><table className="tbl">
              <thead><tr><th>Created</th><th>Signal</th><th>State</th><th>Side</th><th className="text-right">Volume</th><th className="text-right">Price</th><th className="text-right">SL / TP</th><th>Reason</th><th className="text-right">Net</th></tr></thead>
              <tbody>{v.execution.orders.map((o: View) => (
                <tr key={o.key}><td className="num">{fmtShort(o.created_at)}</td><td className="num">{o.signal_id}</td><td>{o.state}</td><td>{o.side ?? dash}</td>
                  <td className="num text-right">{o.volume ?? dash}</td><td className="num text-right">{o.price ?? dash}</td><td className="num text-right">{o.sl ?? dash} / {o.tp ?? dash}</td>
                  <td className="text-mute">{o.reason ?? dash}</td><td className="num text-right">{o.net_result ?? dash}</td></tr>))}</tbody>
            </table></div>
          )}
        </Panel>
      </div>
      <Panel title={<span className="flex items-center gap-2">Broker balance &amp; equity over time <KindTag kind="observed" /></span>}>
        {!(v.series?.points) ? <Empty>No balance/equity observations recorded.</Empty> : (
          <EquityChart books={[
            { book: "balance", strategy: "", initial_equity: 0, commission: 0, points: v.series.points, trades: 0, marks: 0, realized_pnl: 0, realized: [], equity: v.series.balance, drawdown_pct: [], last: 0, max_drawdown_pct: 0 },
            { book: "equity", strategy: "", initial_equity: 0, commission: 0, points: v.series.points, trades: 0, marks: 0, realized_pnl: 0, realized: [], equity: v.series.equity, drawdown_pct: [], last: 0, max_drawdown_pct: 0 }]} height={240} />
        )}
        <p className="mt-2 text-[11px] text-mute">{v.series?.points ?? 0} observations ({v.series?.source}). Gaps are not interpolated. Demo money.</p>
      </Panel>
      <Panel title="Open broker positions">
        {!(v.open_positions?.positions?.length) ? <Empty>No open positions at the broker.</Empty> : (
          <table className="tbl"><thead><tr><th>Ticket</th><th>Side</th><th className="text-right">Volume</th><th className="text-right">Open → now</th><th className="text-right">SL / TP</th><th className="text-right">Floating</th></tr></thead>
            <tbody>{v.open_positions.positions.map((p: View) => (
              <tr key={p.ticket}><td className="num">{p.ticket}</td><td>{p.side}</td><td className="num text-right">{p.volume}</td><td className="num text-right">{fmtNum(p.open_price)} → {fmtNum(p.current)}</td>
                <td className="num text-right">{p.sl ?? <span className="text-down">none</span>} / {p.tp ?? <span className="text-down">none</span>}</td><td className={`num text-right ${tone(p.floating)}`}>{fmtSigned(p.floating)}</td></tr>))}</tbody></table>
        )}
      </Panel>
      {v.performance && <PerformancePanel p={v.performance} currency={a?.currency} capitalLabel="first deposit" />}
      <Panel title="Closed broker trades (positions reconstructed from broker deals)"><TradesTable trades={v.closed_trades} empty="No broker trades: no demo order has ever been placed on this account." /></Panel>
    </div>
  );
}

function Shadow({ v, setStrategy }: { v: View; setStrategy: (s: string) => void }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {v.strategies?.map((s: View) => <button key={s.id} className={`btn ${v.strategy === s.id ? "btn-on" : ""}`} onClick={() => setStrategy(s.id)} disabled={!s.available}>{s.name}</button>)}
      </div>
      <div className="rounded-xl border border-sim/40 bg-sim/10 px-3 py-2 text-[12.5px] text-sim">
        <b>Simulated shadow book</b> {v.book} · no order was placed · not money · {v.forward_signals} forward signals, {v.warmup_signals} warm-up (excluded)
      </div>
      {!v.available ? <Empty>{v.note ?? "Not available."}</Empty> : (
        <>
          <div className="grid grid-cols-2 gap-4 rounded-2xl border border-line bg-ink-900/80 p-4 sm:grid-cols-4">
            <Stat label="Simulated equity" kind="simulated" value={fmtMoney(v.equity_marked)} sub={v.equity_marked_at ? `marked ${fmtShort(v.equity_marked_at)}` : "no marks yet (flat)"} />
            <Stat label="Starting equity" value={fmtMoney(v.initial_equity)} />
            <Stat label="Open positions" value={v.open_positions?.length ?? 0} sub="floating marked at bar closes" />
            <Stat label="Rejected opportunities" value={v.rejected?.length ?? 0} sub="risk-engine rejections" />
          </div>
          <Panel title="Open simulated positions (entry, stop, target)">
            {!(v.open_positions?.length) ? <Empty>No open simulated position.</Empty> : (
              <table className="tbl"><thead><tr><th>Opened</th><th>Signal</th><th>Side</th><th className="text-right">Size</th><th className="text-right">Entry</th><th className="text-right">Stop</th><th className="text-right">Target</th><th className="text-right">Planned risk</th></tr></thead>
                <tbody>{v.open_positions.map((p: View) => (
                  <tr key={p.book}><td className="num">{fmtShort(p.opened_at)}</td><td className="num">{p.signal_id}</td><td>{p.side}</td><td className="num text-right">{p.size}</td>
                    <td className="num text-right">{fmtNum(Number(p.entry))}</td><td className="num text-right">{fmtNum(Number(p.stop))}</td><td className="num text-right">{p.take_profit ? fmtNum(Number(p.take_profit)) : dash}</td><td className="num text-right">{fmtMoney(Number(p.planned_risk))}</td></tr>))}</tbody></table>
            )}
            <p className="mt-2 text-[11px] text-mute">Floating P&amp;L of shadow positions is marked at bar closes (see simulated equity).</p>
          </Panel>
          <PerformancePanel p={v.performance} capitalLabel="starting equity" />
          <Panel title="Closed simulated trades (click for details)"><TradesTable trades={v.closed_trades} empty="No closed forward-test trades yet. Warm-up signals are never trades." /></Panel>
          <Panel title="Why trades were not taken (risk-engine rejections)">
            {!(v.rejected?.length) ? <Empty>No rejected forward signals.</Empty> : (
              <table className="tbl"><tbody>{v.rejected.map((r: View, i: number) => <tr key={i}><td className="num">{fmtShort(r.at)}</td><td className="num">{r.signal_id}</td><td>{r.reasons.join("; ")}</td></tr>)}</tbody></table>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

function Historical({ v, setStrategy }: { v: View; setStrategy: (s: string) => void }) {
  return (
    <div className="space-y-4">
      <select className="input min-w-[20rem]" value={v.selected ?? ""} onChange={(e) => setStrategy(e.target.value)} aria-label="Research dataset">
        {v.datasets?.map((d: View) => <option key={d.id} value={d.id}>{d.id} · {d.trades} trades · {d.file}</option>)}
      </select>
      <div className="rounded-xl border border-warn/40 bg-warn/10 px-3 py-2 text-[12.5px] text-warn">
        <b>Historical research backtest</b> (examined holdout {fmtShort(v.period?.[0])} → {fmtShort(v.period?.[1])}; report book {fmtMoney(v.initial_equity, 0)}). Not an account and not forward evidence. {v.costs_note}
      </div>
      {v.available && <PerformancePanel p={v.performance} capitalLabel="report book" />}
      {v.available && <Panel title="Recorded holdout trades (click for details)"><TradesTable trades={v.closed_trades} empty="No trades." /></Panel>}
    </div>
  );
}

function Live({ v }: { v: View }) {
  return (
    <Panel title="Real-money trading - future capability only">
      <div className="mb-3 rounded-xl border border-line bg-ink-950/60 px-3 py-3 text-[13px] text-slate-300">{v.note} The dashboard has no order controls in any mode.</div>
      <div className="text-[10.5px] font-semibold uppercase tracking-wider text-mute">Separate evidence-based approval gate (all required)</div>
      <ol className="mt-2 space-y-1.5 text-[12.5px]">{v.approval_gate?.map((g: View, i: number) => (
        <li key={i} className="flex gap-2"><span className="w-5 shrink-0 text-mute">{i + 1}.</span><span className="flex-1 text-slate-300">{g.requirement}</span><StateBadge state="blocked" label={g.status} /></li>))}</ol>
    </Panel>
  );
}

export default function AccountPage() {
  const [mode, setMode] = useState("broker");
  const [strategy, setStrategy] = useState<string>("");
  const q = strategy && (mode === "shadow" || mode === "historical") ? `?strategy=${encodeURIComponent(strategy)}` : "";
  const { data, error, loading, updatedAt } = usePoll<View>(`/api/account/${mode}${q}`, mode === "broker" ? 15000 : 30000);
  return (
    <div className="space-y-4">
      <ErrorNote error={error} updatedAt={updatedAt} />
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-50">Scalping &amp; account performance</h1>
          <p className="max-w-3xl text-[12px] text-mute">Four separate account modes; balances are never mixed. Read-only: no button here can place, change or close an order.</p>
        </div>
      </div>
      <div className="flex gap-1 overflow-x-auto border-b border-line" role="tablist">
        {MODES.map((m) => (
          <button key={m.id} role="tab" aria-selected={mode === m.id} onClick={() => { setMode(m.id); setStrategy(""); }}
            className={`-mb-px flex shrink-0 items-center gap-2 border-b-2 px-3 py-2 text-[13px] ${mode === m.id ? "border-info text-slate-50" : "border-transparent text-mute hover:text-slate-200"}`}>
            {m.label} <KindTag kind={m.kind} />
          </button>
        ))}
      </div>
      <Panel title="XAUUSD scalping research status">
        <p className="text-[12.5px] text-slate-300">No scalping configuration has shown a positive net expectancy. All tested variants (H1-led, 4H-only, 4H+1H) were <b>rejected</b> on unexamined 2019–2020 data (mean net R −0.13 to −0.22; gross edge ≈ 0 before costs). No scalping strategy is in shadow or demo trading. See docs/xau-scalping-research-status.md.</p>
      </Panel>
      {loading && !data ? <Loading rows={2} /> : !data ? null : mode === "broker" ? <Broker v={data} /> : mode === "shadow" ? <Shadow v={data} setStrategy={setStrategy} />
        : mode === "historical" ? <Historical v={data} setStrategy={setStrategy} /> : <Live v={data} />}
    </div>
  );
}
