"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { usePoll } from "@/lib/api";
import { fmtAge, fmtNum, localZoneName, setTzMode, type TzMode } from "@/lib/format";
import type { Overview } from "@/lib/types";
import { IconBrain, IconCandles, IconGrid, IconHistory, IconTarget } from "@/components/icons";

const NAV = [
  { href: "/", label: "Overview", icon: IconGrid },
  { href: "/charts/", label: "Charts", icon: IconCandles },
  { href: "/strategies/", label: "Strategies", icon: IconTarget },
  { href: "/history/", label: "History", icon: IconHistory },
  { href: "/grok/", label: "Grok Room", icon: IconBrain },
];

const TzContext = createContext<{ tz: TzMode; setTz: (t: TzMode) => void }>({ tz: "utc", setTz: () => {} });
export const useTz = () => useContext(TzContext);

function Clock({ tz }: { tz: TzMode }) {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!now) return null;
  const t = tz === "utc" ? now.toISOString().slice(11, 19) : now.toTimeString().slice(0, 8);
  return <span className="num text-slate-200">{t}</span>;
}

function Ticker() {
  const { data, error } = usePoll<Overview>("/api/overview", 15000);
  if (!data) return error ? <span className="text-[11px] text-down">API offline</span> : null;
  return (
    <div className="flex items-center gap-2">
      {Object.values(data.markets).map((m) => {
        const q = m.last_quote;
        const ok = m.runner === "running";
        return (
          <Link key={m.market} href="/charts/" title={`${m.label}: runner ${m.runner}${q ? `, quote ${fmtAge(q.age_s)} old` : ""}`}
            className="group flex items-center gap-2 rounded-lg border border-line bg-ink-900/70 px-2.5 py-1 transition hover:border-ink-600">
            <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-up shadow-[0_0_8px_rgba(38,166,154,.8)]" : "bg-down"}`} />
            <span className="text-[11px] font-semibold text-mute group-hover:text-slate-300">{m.label}</span>
            <span className="num text-[12px] text-slate-100">{q ? fmtNum((q.bid + q.ask) / 2, 2) : "—"}</span>
          </Link>
        );
      })}
    </div>
  );
}

export default function Shell({ children }: { children: ReactNode }) {
  const path = usePathname() || "/";
  const [tz, setTzState] = useState<TzMode>("utc");
  useEffect(() => {
    try {
      const saved = localStorage.getItem("kcc.tz");
      if (saved === "local" || saved === "utc") { setTzMode(saved); setTzState(saved); }
    } catch {}
  }, []);
  const setTz = (t: TzMode) => {
    setTzMode(t);
    setTzState(t);
    try { localStorage.setItem("kcc.tz", t); } catch {}
  };
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href.replace(/\/$/, "")));
  return (
    <TzContext.Provider value={{ tz, setTz }}>
      <div className="min-h-screen bg-[radial-gradient(1200px_600px_at_10%_-10%,rgba(79,156,249,0.08),transparent),radial-gradient(900px_500px_at_100%_0%,rgba(180,140,242,0.06),transparent)]">
        <header className="sticky top-0 z-30 border-b border-line/80 bg-ink-950/80 backdrop-blur-xl">
          <div className="mx-auto flex max-w-[1680px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5">
            <Link href="/" className="flex items-center gap-2.5">
              <div className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-info via-sim to-pink-400 text-[14px] font-black text-ink-950 shadow-[0_0_24px_rgba(79,156,249,.35)]">K</div>
              <div className="leading-tight">
                <div className="text-[14px] font-semibold tracking-tight text-slate-50">Kronos <span className="font-normal text-mute">Command Center</span></div>
                <div className="text-[9.5px] uppercase tracking-[0.22em] text-mute">Shadow forward tests · read-only</div>
              </div>
            </Link>
            <div className="order-3 hidden xl:block"><Ticker /></div>
            <div className="ml-auto flex items-center gap-2 text-xs">
              <span title="No write endpoint: the dashboard cannot place orders, change runners or activate agents."
                className="hidden rounded-full border border-up/40 bg-up/10 px-2.5 py-0.5 font-semibold text-up sm:inline">READ-ONLY · NO ORDERS</span>
              <div className="flex items-center rounded-lg border border-line bg-ink-900 p-0.5" role="group" aria-label="Time zone">
                {(["utc", "local"] as const).map((t) => (
                  <button key={t} onClick={() => setTz(t)} title={t === "utc" ? "Coordinated Universal Time" : localZoneName()}
                    className={`rounded-md px-2 py-0.5 text-[11px] font-medium transition ${tz === t ? "bg-ink-700 text-slate-100" : "text-mute hover:text-slate-300"}`}>
                    {t === "utc" ? "UTC" : "Local"}
                  </button>
                ))}
              </div>
              <Clock tz={tz} />
            </div>
          </div>
          <nav className="mx-auto flex max-w-[1680px] gap-1 overflow-x-auto px-3 pb-2" aria-label="Main">
            {NAV.map(({ href, label, icon: Icon }) => (
              <Link key={href} href={href}
                className={`flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] transition ${active(href) ? "bg-gradient-to-b from-ink-700 to-ink-800 text-slate-50 shadow-[inset_0_1px_0_rgba(255,255,255,.06)]" : "text-mute hover:bg-ink-800/70 hover:text-slate-200"}`}>
                <Icon size={15} /> {label}
              </Link>
            ))}
          </nav>
        </header>
        <main key={tz} className="mx-auto max-w-[1680px] px-4 py-5">{children}</main>
        <footer className="mx-auto max-w-[1680px] px-4 pb-8 text-[11px] text-mute">
          Simulated results come from shadow books. No order has been placed on MT5, Binance or any testnet.
          Times shown in {tz === "utc" ? "UTC" : localZoneName()}; all records are stored in UTC.
        </footer>
      </div>
    </TzContext.Provider>
  );
}
