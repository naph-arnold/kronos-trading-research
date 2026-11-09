"use client";

import { useMemo, useState } from "react";
import type { ReactNode } from "react";

/** The four kinds of information the dashboard distinguishes everywhere. */
export type Kind = "observed" | "simulated" | "derived" | "unavailable";

const KIND_STYLE: Record<Kind, string> = {
  observed: "border-info/40 bg-info/10 text-info",
  simulated: "border-sim/40 bg-sim/10 text-sim",
  derived: "border-slate-500/40 bg-slate-500/10 text-slate-300",
  unavailable: "border-slate-600/50 bg-transparent text-slate-500",
};

const KIND_TITLE: Record<Kind, string> = {
  observed: "Observed: recorded by a runner from the venue (read-only).",
  simulated: "Simulated: shadow-book result. No order was placed.",
  derived: "Derived: computed by the dashboard from recorded data.",
  unavailable: "Unavailable: not recorded. Nothing is filled in.",
};

export function KindTag({ kind }: { kind: Kind }) {
  return (
    <span title={KIND_TITLE[kind]}
      className={`inline-flex items-center rounded border px-1.5 py-px text-[9.5px] font-semibold uppercase tracking-wider ${KIND_STYLE[kind]}`}>
      {kind}
    </span>
  );
}

const STATE_STYLE: Record<string, string> = {
  running: "bg-up/15 text-up border-up/40",
  connected: "bg-up/15 text-up border-up/40",
  allowed: "bg-up/15 text-up border-up/40",
  fresh: "bg-up/15 text-up border-up/40",
  ok: "bg-up/15 text-up border-up/40",
  "within limits": "bg-up/15 text-up border-up/40",
  prepared: "bg-info/15 text-info border-info/40",
  "heartbeat delayed": "bg-warn/15 text-warn border-warn/40",
  paused: "bg-warn/15 text-warn border-warn/40",
  blocked: "bg-down/15 text-down border-down/40",
  halted: "bg-down/15 text-down border-down/40",
  stopped: "bg-down/15 text-down border-down/40",
  "not responding": "bg-down/15 text-down border-down/40",
  disconnected: "bg-down/15 text-down border-down/40",
  stale: "bg-warn/15 text-warn border-warn/40",
  closed: "bg-slate-500/15 text-slate-300 border-slate-500/40",
  unavailable: "bg-down/15 text-down border-down/40",
  "data unavailable": "bg-down/15 text-down border-down/40",
  "not activated": "bg-info/15 text-info border-info/40",
};

export function StateBadge({ state, label }: { state: string | null | undefined; label?: string }) {
  const s = state ?? "unknown";
  const style = STATE_STYLE[s] ?? "bg-slate-600/15 text-slate-400 border-slate-600/40";
  const dot = style.includes("text-up") ? "bg-up" : style.includes("text-down") ? "bg-down"
    : style.includes("text-warn") ? "bg-warn" : style.includes("text-info") ? "bg-info" : "bg-slate-500";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${style}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot} ${s === "running" ? "animate-pulse" : ""}`} />
      {label ?? s}
    </span>
  );
}

export function Panel({ title, right, children, className = "" }: { title: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-h">
        <h2 className="panel-t">{title}</h2>
        {right && <div className="flex items-center gap-2">{right}</div>}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, kind, valueClass = "" }: { label: string; value: ReactNode; sub?: ReactNode; kind?: Kind; valueClass?: string }) {
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5 text-[10.5px] font-medium uppercase tracking-wider text-mute">
        {label} {kind && <KindTag kind={kind} />}
      </div>
      <div className={`num mt-0.5 truncate text-lg font-semibold text-slate-100 ${valueClass}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] leading-snug text-mute">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-dashed border-line px-3 py-3 text-[12.5px] text-mute">
      <KindTag kind="unavailable" /> <span>{children}</span>
    </div>
  );
}

export function ErrorNote({ error, updatedAt }: { error: string | null; updatedAt?: Date | null }) {
  if (!error) return null;
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2 rounded-xl border border-down/40 bg-down/10 px-3 py-2 text-xs text-down">
      <span className="font-semibold">Dashboard API unreachable</span> <span className="text-down/80">({error}).</span>
      {updatedAt ? <span>Showing the last data received at {updatedAt.toISOString().slice(11, 19)} UTC; nothing is extrapolated.</span>
        : <span>No data could be loaded. Is the Command Center server running?</span>}
    </div>
  );
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="Loading recorded data">
      <div className="skeleton h-6 w-48" />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {Array.from({ length: rows }).map((_, i) => <div key={i} className="skeleton h-40" />)}
      </div>
    </div>
  );
}

export interface Column<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  sort?: (row: T) => number | string | null;
  align?: "right";
  className?: string;
}

/** Sortable table (click a header). Sorting is local to the rows already loaded. */
export function DataTable<T>({ rows, columns, onRow, rowKey, empty, maxH = "max-h-[560px]", initialSort }: {
  rows: T[]; columns: Column<T>[]; onRow?: (r: T) => void; rowKey: (r: T, i: number) => string;
  empty?: ReactNode; maxH?: string; initialSort?: { key: string; dir: 1 | -1 };
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(initialSort ?? null);
  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort?.key);
    if (!col?.sort || !sort) return rows;
    const f = col.sort;
    return [...rows].sort((a, b) => {
      const x = f(a), y = f(b);
      if (x === y) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (x < y ? -1 : 1) * sort.dir;
    });
  }, [rows, columns, sort]);
  if (!rows.length) return <>{empty ?? <Empty>No recorded rows match.</Empty>}</>;
  return (
    <div className={`${maxH} overflow-auto rounded-lg border border-line/70`}>
      <table className="tbl">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.align === "right" ? "text-right" : ""}>
                {c.sort ? (
                  <button className="inline-flex items-center gap-1 uppercase tracking-wider hover:text-slate-200"
                    onClick={() => setSort(sort?.key === c.key ? { key: c.key, dir: sort.dir === 1 ? -1 : 1 } : { key: c.key, dir: -1 })}>
                    {c.label}<span className="text-[9px]">{sort?.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : "↕"}</span>
                  </button>
                ) : c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => (
            <tr key={rowKey(r, i)} onClick={onRow ? () => onRow(r) : undefined} className={onRow ? "cursor-pointer" : ""}>
              {columns.map((c) => <td key={c.key} className={`${c.align === "right" ? "num text-right" : ""} ${c.className ?? ""}`}>{c.render(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
