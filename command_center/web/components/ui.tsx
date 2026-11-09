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

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-xs text-down">
      Dashboard API unreachable: {error}. Displayed values may be outdated; nothing is extrapolated.
    </div>
  );
}

export function Loading() {
  return <div className="animate-pulse text-sm text-mute">Loading recorded data…</div>;
}
