"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { usePoll } from "@/lib/api";
import { dash, fmtMoney, fmtNum, fmtPct, fmtShort, fmtSigned, tone } from "@/lib/format";
import type { EquityData, GrokRoom, HDecision, HEvent, HistoryPage, HSignal, HTrade, Meta } from "@/lib/types";
import { DataTable, Empty, ErrorNote, KindTag, Panel, PhaseTag, Stat, StateBadge, type Column } from "@/components/ui";
import { IconSearch } from "@/components/icons";

const EquityChart = dynamic(() => import("@/components/EquityChart"), { ssr: false });

const TABS = [
  { id: "signals", label: "Signals" }, { id: "decisions", label: "Risk decisions" }, { id: "trades", label: "Simulated trades" },
  { id: "equity", label: "Equity & drawdown" }, { id: "events", label: "Interruptions & alerts" }, { id: "grok", label: "Grok advisory" },
] as const;
type Tab = (typeof TABS)[number]["id"];

const STATUS: Record<string, [string, string][]> = {
  signals: [["", "Forward + warm-up"], ["forward", "Forward test only"], ["warmup", "Warm-up only (not evidence)"], ["evaluated", "Forward · evaluated"], ["pending", "Forward · pending"], ["expired", "Forward · expired"], ["rejected_unanchored", "Rejected (unanchored)"]],
  decisions: [["", "All decisions"], ["approved", "Approved"], ["rejected", "Rejected"]],
  trades: [["", "All exits"], ["stop", "Stop"], ["take_profit", "Take profit"], ["time_exit", "Time exit"]],
  events: [["", "All events"], ["interruption", "Interruptions"], ["alert", "Alerts"], ["routine", "Routine"]],
};

function useDebounced<T>(v: T, ms = 300): T {
  const [d, setD] = useState(v);
  useEffect(() => { const id = setTimeout(() => setD(v), ms); return () => clearTimeout(id); }, [v, ms]);
  return d;
}

const sideCell = (s: string | null) => <span className={s === "long" ? "text-up" : s === "short" ? "text-down" : ""}>{s ?? dash}</span>;

export default function HistoryPage() {
  const router = useRouter();
  const meta = usePoll<Meta>("/api/meta", 0);
  const [runner, setRunner] = useState("xauusd");
  const [tab, setTab] = useState<Tab>("signals");
  const [q, setQ] = useState("");
  const [strategy, setStrategy] = useState("");
  const [status, setStatus] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [page, setPage] = useState(0);
  const PAGE = 100;
  const dq = useDebounced(q);
  useEffect(() => setPage(0), [runner, tab, dq, strategy, status, start, end]);
  useEffect(() => setStatus(""), [tab]);

  const isList = tab === "signals" || tab === "decisions" || tab === "trades" || tab === "events";
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
  if (dq) params.set("q", dq);
  if (strategy) params.set("strategy", strategy);
  if (status) params.set("status", status);
  if (start) params.set("start", `${start}T00:00:00`);
  if (end) params.set("end", `${end}T23:59:59.999999+00:00`);
  type AnyRow = { strategy?: string | null } & Record<string, unknown>;
  const list = usePoll<HistoryPage<AnyRow>>(isList ? `/api/history/${runner}/${tab}?${params}` : null, 30000);
  const eq = usePoll<EquityData>(tab === "equity" ? `/api/equity/${runner}${strategy ? `?strategy=${strategy}` : ""}` : null, 60000);
  const grok = usePoll<GrokRoom>(tab === "grok" ? "/api/grok" : null, 60000);

  const runners = meta.data?.runners ?? { xauusd: { label: "XAUUSD", market: "xauusd" }, btcusdt: { label: "BTC/USDT", market: "btcusdt" } };
  const strategyKeys = useMemo(() => {
    const keys = new Set<string>();
    (list.data?.rows ?? []).forEach((r) => r.strategy && keys.add(r.strategy));
    meta.data?.strategies.filter((s) => s.market === runners[runner]?.market).forEach((s) => keys.add(s.strategy_key));
    return [...keys].sort();
  }, [list.data, meta.data, runner, runners]);

  const open = (signalId: string) => router.push(`/signal/?runner=${runner}&id=${encodeURIComponent(signalId)}`);

  const sigCols: Column<HSignal>[] = [
    { key: "at", label: "Bar close", render: (r) => <span className="num whitespace-nowrap">{fmtShort(r.at)}</span>, sort: (r) => r.at },
    { key: "strategy", label: "Strategy", render: (r) => <span className={r.strategy.startsWith("random") ? "text-mute" : ""}>{r.strategy}</span>, sort: (r) => r.strategy },
    { key: "side", label: "Side", render: (r) => sideCell(r.side), sort: (r) => r.side },
    { key: "phase", label: "Phase", render: (r) => <PhaseTag status={r.status} compact />, sort: (r) => (r.status === "warmup" ? 0 : 1) },
    { key: "status", label: "Status", render: (r) => <span className={r.status === "warmup" ? "text-mute" : "text-slate-200"}>{r.status === "warmup" ? "—" : r.status}</span>, sort: (r) => r.status },
    { key: "entry", label: "Entry", align: "right", render: (r) => fmtNum(r.entry), sort: (r) => r.entry },
    { key: "stop", label: "Stop", align: "right", render: (r) => fmtNum(r.stop) },
    { key: "tp", label: "Target", align: "right", render: (r) => fmtNum(r.take_profit) },
    { key: "dec", label: "Approved / rejected", align: "right", render: (r) => `${r.approved} / ${r.rejected}`, sort: (r) => r.approved },
    { key: "traded", label: "Traded", render: (r) => (r.traded ? <span className="text-sim">yes</span> : <span className="text-mute">no</span>) },
  ];
  const decCols: Column<HDecision>[] = [
    { key: "at", label: "Evaluated", render: (r) => <span className="num whitespace-nowrap">{fmtShort(r.at)}</span>, sort: (r) => r.at },
    { key: "book", label: "Book", render: (r) => <span className="num">{r.book}</span>, sort: (r) => r.book },
    { key: "d", label: "Decision", render: (r) => <StateBadge state={r.approved ? "allowed" : "blocked"} label={r.approved ? "approved" : "rejected"} />, sort: (r) => (r.approved ? 1 : 0) },
    { key: "size", label: "Size", align: "right", render: (r) => fmtNum(r.size, 3) },
    { key: "risk", label: "Planned risk", align: "right", render: (r) => fmtMoney(r.planned_risk), sort: (r) => r.planned_risk },
    { key: "why", label: "Reasons", render: (r) => <span className="text-slate-300">{r.reasons.join("; ") || dash}</span>, className: "min-w-[260px]" },
  ];
  const tradeCols: Column<HTrade>[] = [
    { key: "at", label: "Closed", render: (r) => <span className="num whitespace-nowrap">{fmtShort(r.at)}</span>, sort: (r) => r.at },
    { key: "book", label: "Book", render: (r) => <span className="num">{r.book}</span>, sort: (r) => r.book },
    { key: "side", label: "Side", render: (r) => sideCell(r.side) },
    { key: "entry", label: "Entry → exit", align: "right", render: (r) => `${fmtNum(r.entry)} → ${fmtNum(r.exit)}` },
    { key: "gross", label: "Gross", align: "right", render: (r) => <span className={tone(r.gross_pnl)}>{fmtNum(r.gross_pnl)}</span>, sort: (r) => r.gross_pnl },
    { key: "costs", label: "Costs", align: "right", render: (r) => fmtNum((r.costs.spread_slippage_cost ?? 0) + (r.costs.commission ?? 0) + (r.costs.swap ?? 0)), sort: (r) => (r.costs.spread_slippage_cost ?? 0) + (r.costs.commission ?? 0) },
    { key: "pnl", label: "Net", align: "right", render: (r) => <span className={tone(r.pnl)}>{fmtNum(r.pnl)}</span>, sort: (r) => r.pnl },
    { key: "r", label: "R", align: "right", render: (r) => <span className={tone(r.r)}>{fmtSigned(r.r)}</span>, sort: (r) => r.r },
    { key: "mfe", label: "MFE / MAE R", align: "right", render: (r) => `${fmtNum(r.mfe_r)} / ${fmtNum(r.mae_r)}` },
    { key: "exit", label: "Exit", render: (r) => r.exit_reason ?? dash, sort: (r) => r.exit_reason },
  ];
  const evCols: Column<HEvent>[] = [
    { key: "at", label: "Time", render: (r) => <span className="num whitespace-nowrap">{fmtShort(r.at)}</span>, sort: (r) => r.at },
    { key: "class", label: "Class", render: (r) => <StateBadge state={r.class === "interruption" ? "paused" : r.class === "alert" ? "blocked" : "closed"} label={r.class} />, sort: (r) => r.class },
    { key: "event", label: "Event", render: (r) => <span className="num">{r.event}</span>, sort: (r) => r.event },
    { key: "detail", label: "Detail", render: (r) => <span className="break-all text-[11.5px] text-mute">{r.detail}</span>, className: "min-w-[300px]" },
  ];

  const total = list.data?.total ?? 0;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-50">History & analytics</h1>
          <p className="text-[12px] text-mute">Search every recorded signal, risk decision, simulated trade, equity mark, interruption and advisory review. Click a row to open the full signal record.</p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(runners).map(([id, r]) => (
            <button key={id} className={`btn ${runner === id ? "btn-on" : ""}`} onClick={() => { setRunner(id); setStrategy(""); }}>{r.label}</button>
          ))}
        </div>
      </div>
      <div className="flex gap-1 overflow-x-auto border-b border-line" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}
            className={`-mb-px shrink-0 border-b-2 px-3 py-2 text-[13px] transition ${tab === t.id ? "border-info text-slate-50" : "border-transparent text-mute hover:text-slate-200"}`}>{t.label}</button>
        ))}
      </div>

      {tab !== "grok" && (
        <div className="flex flex-wrap items-center gap-2">
          {isList && (
            <label className="relative">
              <IconSearch size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-mute" />
              <input className="input w-64 pl-8" placeholder="Search id, book, reason, event…" value={q} onChange={(e) => setQ(e.target.value)} />
            </label>
          )}
          {tab !== "events" && (
            <select className="input" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              <option value="">All strategies</option>
              {strategyKeys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          )}
          {STATUS[tab] && (
            <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUS[tab].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          )}
          {isList && (
            <>
              <input type="date" className="input" value={start} onChange={(e) => setStart(e.target.value)} aria-label="From (UTC)" />
              <span className="text-mute">→</span>
              <input type="date" className="input" value={end} onChange={(e) => setEnd(e.target.value)} aria-label="To (UTC)" />
              <span className="text-[11px] text-mute">dates in UTC</span>
            </>
          )}
          {(q || strategy || status || start || end) && <button className="btn" onClick={() => { setQ(""); setStrategy(""); setStatus(""); setStart(""); setEnd(""); }}>Reset</button>}
        </div>
      )}

      {isList && (
        <Panel title={<span className="flex items-center gap-2">{TABS.find((t) => t.id === tab)?.label} <KindTag kind={tab === "events" ? "observed" : tab === "signals" ? "derived" : "simulated"} /></span>}
          right={<span className="text-[11px] text-mute">{total} recorded · page {page + 1} of {Math.max(1, Math.ceil(total / PAGE))}</span>}>
          <ErrorNote error={list.error} updatedAt={list.updatedAt} />
          {!list.data ? <div className="skeleton h-64" /> : !list.data.available ? <Empty>{list.data.note}</Empty> : (
            <>
              {tab === "signals" && (
                <>
                  <p className="mb-2 text-[11.5px] text-mute"><PhaseTag status="forward" compact /> generated live after the pre-registered boundary (evidence) · <PhaseTag status="warmup" compact /> generated on history before it to prime the strategy (never evaluated, never traded).</p>
                  <DataTable rows={list.data.rows as unknown as HSignal[]} columns={sigCols} rowKey={(r) => r.signal_id} onRow={(r) => open(r.signal_id)}
                    empty={<Empty>{status === "forward" ? "No forward-test signals recorded yet." : "No recorded signals match these filters."}</Empty>} />
                </>
              )}
              {tab === "decisions" && <DataTable rows={list.data.rows as unknown as HDecision[]} columns={decCols} rowKey={(r) => r.book + r.signal_id} onRow={(r) => open(r.signal_id)}
                empty={<Empty>No risk decisions recorded{total === 0 && !dq ? " yet: no forward signal has reached the risk engine" : " for these filters"}.</Empty>} />}
              {tab === "trades" && <DataTable rows={list.data.rows as unknown as HTrade[]} columns={tradeCols} rowKey={(r) => r.book + r.signal_id} onRow={(r) => open(r.signal_id)}
                empty={<Empty>No simulated trades recorded{total === 0 && !dq ? " yet" : " for these filters"}. None are invented.</Empty>} />}
              {tab === "events" && <DataTable rows={list.data.rows as unknown as HEvent[]} columns={evCols} rowKey={(r, i) => r.at + r.event + i}
                empty={<Empty>No events match.</Empty>} />}
              {total > PAGE && (
                <div className="mt-3 flex items-center justify-end gap-2">
                  <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Newer</button>
                  <button className="btn" disabled={(page + 1) * PAGE >= total} onClick={() => setPage(page + 1)}>Older →</button>
                </div>
              )}
            </>
          )}
        </Panel>
      )}

      {tab === "equity" && (
        <Panel title={<span className="flex items-center gap-2">Simulated equity & drawdown <KindTag kind="simulated" /> <KindTag kind="derived" /></span>}>
          <ErrorNote error={eq.error} updatedAt={eq.updatedAt} />
          {!eq.data ? <div className="skeleton h-80" /> : !eq.data.available ? <Empty>Runner not started: no equity recorded.</Empty> : (
            <EquityPanel data={eq.data} />
          )}
        </Panel>
      )}

      {tab === "grok" && (
        <Panel title="Recorded Grok advisory responses">
          {!grok.data ? <div className="skeleton h-40" /> : grok.data.stores.length === 0 ? (
            <Empty>No advisory responses recorded. The agents are {grok.data.status}; paid API calls are disabled.</Empty>
          ) : (
            <div className="space-y-4">
              {grok.data.stores.map((s) => (
                <div key={s.dir} className="space-y-2">
                  <div className="text-[11.5px] text-mute">{s.dir} · audit {s.audit?.ok ? <span className="text-up">verified</span> : <span className="text-down">{s.audit?.problems.join("; ")}</span>}</div>
                  <DataTable rows={s.outputs} rowKey={(r, i) => r.subject + r.role + i}
                    columns={[
                      { key: "at", label: "Time", render: (r) => fmtShort(r.at), sort: (r) => r.at },
                      { key: "subject", label: "Subject", render: (r) => <span className="num">{r.subject}</span> },
                      { key: "role", label: "Role", render: (r) => r.role, sort: (r) => r.role },
                      { key: "valid", label: "Valid", render: (r) => (r.valid ? <span className="text-up">valid</span> : <span className="text-down">rejected</span>) },
                      { key: "err", label: "Validation error", render: (r) => <span className="text-mute">{r.error ?? dash}</span> },
                    ]} />
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}
    </div>
  );
}

/** One simulated book at a time: books are alternative simulations of the same signals, never a combined account. */
function EquityPanel({ data }: { data: EquityData }) {
  const ordered = useMemo(() => [...data.books].sort((a, b) =>
    Number(a.strategy.startsWith("random")) - Number(b.strategy.startsWith("random")) || b.initial_equity - a.initial_equity || b.commission - a.commission), [data]);
  const [book, setBook] = useState<string>("");
  useEffect(() => { if (!ordered.some((b) => b.book === book)) setBook(ordered[0]?.book ?? ""); }, [ordered, book]);
  const b = ordered.find((x) => x.book === book);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[12px] text-mute" htmlFor="eqbook">Simulated book</label>
        <select id="eqbook" className="input min-w-[16rem]" value={book} onChange={(e) => setBook(e.target.value)}>
          {ordered.map((x) => <option key={x.book} value={x.book}>{x.book} · ${x.initial_equity.toLocaleString("en-US")} · cost {x.commission}{x.strategy.startsWith("random") ? " (control)" : ""}</option>)}
        </select>
        <span className="rounded-full border border-line px-2 py-0.5 text-[11px] text-mute">one book at a time · never summed</span>
      </div>
      {!b ? <Empty>No simulated books recorded.</Empty> : (
        <>
          <div className="grid grid-cols-2 gap-4 rounded-xl border border-line bg-ink-950/40 p-3 sm:grid-cols-5">
            <Stat label="Starting equity" value={fmtMoney(b.initial_equity, 0)} kind="simulated" />
            <Stat label="Equity now" value={fmtMoney(b.last)} sub={fmtSigned(((b.last / b.initial_equity) - 1) * 100, 2, "%")} valueClass={tone(b.last - b.initial_equity)} />
            <Stat label="Closed trades" value={b.trades} sub={`realized ${fmtSigned(b.realized_pnl)}`} />
            <Stat label="Max drawdown" value={fmtPct(-b.max_drawdown_pct)} kind="derived" />
            <Stat label="Equity marks" value={b.marks} sub="while a position was open" />
          </div>
          {b.points === 0 ? (
            <Empty>No closed simulated trades or equity marks yet for {b.book}: the curve stays at its starting equity of {fmtMoney(b.initial_equity, 0)}. Nothing is drawn until the runner records a trade.</Empty>
          ) : <EquityChart books={[b]} />}
        </>
      )}
      <p className="text-[11px] text-mute">{data.note}</p>
    </div>
  );
}
