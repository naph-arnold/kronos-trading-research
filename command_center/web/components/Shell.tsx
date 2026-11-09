"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/charts/", label: "Charts" },
  { href: "/strategies/", label: "Strategies" },
  { href: "/grok/", label: "Grok Decision Room" },
];

function UtcClock() {
  const [now, setNow] = useState<string>("");
  useEffect(() => {
    const tick = () => setNow(new Date().toISOString().slice(11, 19));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="num text-slate-300">{now} <span className="text-mute">UTC</span></span>;
}

export default function Shell({ children }: { children: ReactNode }) {
  const path = usePathname() || "/";
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href.replace(/\/$/, "")));
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-line bg-ink-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <div className="grid h-7 w-7 place-items-center rounded-md bg-gradient-to-br from-info to-sim text-[13px] font-black text-ink-950">K</div>
            <div className="leading-tight">
              <div className="text-sm font-semibold tracking-wide text-slate-100">Kronos Command Center</div>
              <div className="text-[10px] uppercase tracking-[0.18em] text-mute">Shadow forward tests · V1</div>
            </div>
          </div>
          <nav className="flex flex-wrap gap-1">
            {NAV.map((n) => (
              <Link key={n.href} href={n.href}
                className={`rounded-md px-3 py-1.5 text-[13px] transition ${active(n.href) ? "bg-ink-700 text-slate-100" : "text-mute hover:bg-ink-800 hover:text-slate-200"}`}>
                {n.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-xs">
            <span title="The dashboard has no write endpoint: it cannot place orders, change runners or activate agents."
              className="rounded-full border border-up/40 bg-up/10 px-2.5 py-0.5 font-semibold text-up">READ-ONLY · NO ORDERS</span>
            <UtcClock />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1600px] px-4 py-5">{children}</main>
      <footer className="mx-auto max-w-[1600px] px-4 pb-6 text-[11px] text-mute">
        Simulated results come from shadow books. No order has been placed on MT5, Binance or any testnet. Times are UTC.
      </footer>
    </div>
  );
}
