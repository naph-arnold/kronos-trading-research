"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { usePoll } from "@/lib/api";
import { ageFrom, dash, fmtAge, fmtMoney, fmtNum, fmtPct, fmtRel, fmtShort, fmtSigned, fmtTime, humanize, marketLabel, tone, tzLabel } from "@/lib/format";
import type { Account, Activity, AlertGroups, Decision, Health, MarketStatus, OrbWindow, Overview, RiskStatus, StrategyPanel } from "@/lib/types";
import { DecisionBadge, Empty, ErrorNote, KindTag, Loading, Panel, PhaseTag, Stat, StateBadge } from "@/components/ui";

function MarketCard({ m, h }: { m: MarketStatus; h?: Health }) {
  if (!m.available) {
    return (
      <Panel title={m.label} right={<StateBadge state="blocked" label={m.runner ?? "unavailable"} />}>
        <Empty>{m.runner === "not started" ? "Runner not started. No observations recorded." : "The runner's database could not be read just now (locked, missing or damaged). Nothing is inferred; the dashboard retries automatically."}</Empty>
      </Panel>
    );
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
  if (!L) return <div className="rounded-lg border border-line bg-ink-850 p-3 text-xs text-mute">{name === "xauusd" ? "XAUUSD" : "BTC/USDT"}: risk state unavailable.</div>;
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

function useNow(ms = 30000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}

const CHECK_ICON: Record<string, [string, string]> = { ok: ["✓", "text-up"], blocked: ["✕", "text-warn"], info: ["•", "text-mute"] };

/** Today's ORB window as a small timeline: opening range, entry window and a "now" marker (scheduled, not predicted). */
function OrbTimeline({ w, now }: { w: OrbWindow; now: number }) {
  const t = (s: string) => new Date(s).getTime();
  const lo = t(w.range_from) - 2 * 3600e3, hi = t(w.entry_to) + 3600e3;
  const pct = (x: number) => `${Math.min(100, Math.max(0, ((x - lo) / (hi - lo)) * 100))}%`;
  const inView = now >= lo && now <= hi;
  return (
    <div className="mt-1">
      <div className="relative h-5 rounded bg-ink-950/80">
        <div className="absolute inset-y-0 rounded-l bg-info/35" style={{ left: pct(t(w.range_from)), width: `calc(${pct(t(w.range_to))} - ${pct(t(w.range_from))})` }} title="opening range" />
        <div className="absolute inset-y-0 rounded-r bg-up/25" style={{ left: pct(t(w.entry_from)), width: `calc(${pct(t(w.entry_to))} - ${pct(t(w.entry_from))})` }} title="entry window" />
        {inView && <div className="absolute inset-y-[-3px] w-0.5 bg-warn" style={{ left: pct(now) }} title="now" />}
      </div>
      <div className="mt-1 flex justify-between text-[10.5px] text-mute">
        <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-info/60" />range {fmtTime(w.range_from, false).slice(0, 5)}–{fmtTime(w.range_to, false).slice(0, 5)}</span>
        <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-up/50" />entries until {fmtTime(w.entry_to, false).slice(0, 5)} {tzLabel()}</span>
      </div>
    </div>
  );
}

function DecisionCard({ d, s }: { d: Decision; s?: StrategyPanel }) {
  const now = useNow();
  const [more, setMore] = useState(false);
  const sess = d.session;
  const w = sess?.windows?.[0];
  const st = d.setup;
  const m = s?.metrics;
  const blackouts = d.blackouts?.windows ?? [];
  return (
    <article className="card flex min-w-0 flex-col gap-3 p-4">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[13.5px] font-semibold leading-snug text-slate-100">{d.name}</div>
          <div className="text-[11px] text-mute">{marketLabel(d.market)} · <span className="num">{d.strategy_key}</span></div>
        </div>
        <DecisionBadge state={d.state} label={d.state_label} />
      </header>
      <div>
        <div className="text-[14px] font-medium text-slate-50">{d.headline}</div>
        {d.explanation && <p className="mt-1 text-[12px] leading-relaxed text-slate-400">{humanize(d.explanation)}</p>}
      </div>

      {sess && d.state !== "unavailable" && (
        <div className="rounded-lg border border-line/70 bg-ink-950/40 p-3 text-[12px]">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-[10.5px] font-semibold uppercase tracking-wider text-mute">
              {sess.type === "orb" ? "Next session window" : sess.type === "continuous" ? "Evaluation schedule" : "Market session"}
            </span>
            <KindTag kind="derived" />
          </div>
          {sess.type === "orb" && w ? (
            <>
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <span className="text-slate-200">{w.date} · opening range {w.local_open} New York</span>
                <span className="num text-info">{new Date(w.range_from).getTime() > now ? `range ${fmtRel(w.range_from, now)}` : new Date(w.entry_to).getTime() > now ? `entries close ${fmtRel(w.entry_to, now)}` : ""}</span>
              </div>
              <OrbTimeline w={w} now={now} />
              {w.shortened_by_closure && <div className="mt-1 text-warn">Entry window shortened by a scheduled market closure.</div>}
              {sess.windows && sess.windows.length > 1 && (
                <div className="mt-2 text-[11px] text-mute">Following: {sess.windows.slice(1).map((x) => `${x.date} ${fmtTime(x.range_from, false).slice(0, 5)}`).join(" · ")} {tzLabel()}</div>
              )}
            </>
          ) : sess.type === "continuous" ? (
            <div className="text-slate-300">24/7 market · next hourly bar evaluated <span className="num text-info">{fmtRel(sess.next_evaluation, now)}</span> ({fmtTime(sess.next_evaluation, false).slice(0, 5)} {tzLabel()})</div>
          ) : sess.market ? (
            <div className="text-slate-300">Gold market {sess.market.open ? "open" : "closed"} · next {sess.market.next_change_kind} {fmtShort(sess.market.next_change)} ({fmtRel(sess.market.next_change, now)})</div>
          ) : <div className="text-mute">{sess.error ?? "Calendar unavailable."}</div>}
          {sess.rule && <p className="mt-2 text-[11px] leading-relaxed text-mute">{sess.rule}</p>}
        </div>
      )}

      {st?.available && (
        <div className="rounded-lg border border-line/70 bg-ink-950/40 p-3 text-[12px]">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-[10.5px] font-semibold uppercase tracking-wider text-mute">Current setup (frozen rules)</span>
            <KindTag kind="derived" />
          </div>
          {d.strategy_key.startsWith("orb") ? (
            st.range_high != null ? (
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
                <div className="kv"><dt>Range ({st.session_day})</dt><dd>{fmtNum(st.range_low)} – {fmtNum(st.range_high)}</dd></div>
                <div className="kv"><dt>Width / ATR</dt><dd className={st.range_filter_ok ? "text-up" : "text-warn"}>{fmtNum(st.width_atr)}× ({st.min_range_atr}–{st.max_range_atr}×)</dd></div>
                <div className="kv"><dt>Long above</dt><dd className="text-up">{fmtNum(st.long_trigger)}</dd></div>
                <div className="kv"><dt>Short below</dt><dd className="text-down">{fmtNum(st.short_trigger)}</dd></div>
              </dl>
            ) : <div className="text-slate-400">Today&apos;s opening range has not formed yet (session-day {st.session_day}). ATR(14, M15) {fmtNum(st.atr)}.</div>
          ) : d.strategy_key === "donchian" ? (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
              <div className="kv"><dt>{st.lookback}-bar channel high</dt><dd className="text-up">{fmtNum(st.channel_high)}</dd></div>
              <div className="kv"><dt>Last 1h close</dt><dd>{fmtNum(st.last_close)}</dd></div>
              <div className="kv col-span-2"><dt>Distance to breakout</dt><dd>{fmtNum(st.distance)} ({fmtNum(st.distance_pct)}% below)</dd></div>
            </dl>
          ) : null}
          <p className="mt-1.5 text-[10.5px] text-mute">Replayed from the runner&apos;s {st.bars_replayed} recorded bars (last close {fmtShort(st.last_bar_close)}). Levels move with each new bar; this is not a forecast.</p>
        </div>
      )}

      {d.checks.length > 0 && (
        <ul className="space-y-1 text-[12px]">
          {d.checks.map((c) => {
            const [ic, cls] = CHECK_ICON[c.status] ?? CHECK_ICON.info;
            return (
              <li key={c.check} className="flex items-start gap-2">
                <span className={`w-3 shrink-0 text-center font-bold ${cls}`}>{ic}</span>
                <span className="w-36 shrink-0 text-mute">{c.check}</span>
                <span className="min-w-0 flex-1 break-words text-slate-300">{humanize(c.detail)}</span>
              </li>
            );
          })}
        </ul>
      )}

      {blackouts.length > 0 && (
        <div className="text-[11.5px]">
          <div className="mb-1 flex items-center gap-2 text-[10.5px] font-semibold uppercase tracking-wider text-mute">Scheduled event blackouts <KindTag kind="observed" /></div>
          <ul className="space-y-0.5">
            {blackouts.slice(0, more ? 8 : 2).map((b) => (
              <li key={b.event + b.at} className={b.active ? "text-warn" : "text-slate-400"}>
                {b.active ? "Active now · " : ""}{b.event}: no new entries {fmtShort(b.from)} → {fmtTime(b.to, false).slice(0, 5)} ({fmtRel(b.from, now)})
              </li>
            ))}
          </ul>
          {blackouts.length > 2 && <button className="mt-1 text-info hover:underline" onClick={() => setMore(!more)}>{more ? "fewer" : `${blackouts.length - 2} more`}</button>}
        </div>
      )}

      <footer className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line/70 pt-2 text-[11.5px]">
        {d.signals ? (
          <>
            <span className="flex items-center gap-1"><PhaseTag status="forward" /> <span className="num text-slate-200">{d.signals.forward_count}</span></span>
            <span className="flex items-center gap-1"><PhaseTag status="warmup" compact /> <span className="num text-mute">{d.signals.warmup_count}</span></span>
            {d.signals.forward ? <span className="text-mute">last forward {fmtShort(d.signals.forward.bar_close)}</span> : <span className="text-mute">no forward signal yet</span>}
          </>
        ) : <span className="text-mute">No signals recorded.</span>}
        {m && <span className="ml-auto text-mute">{m.trades} sim. trades · <span className={tone(m.net_pnl)}>{fmtMoney(m.net_pnl)}</span></span>}
        <Link href={`/strategies/#${d.id}`} className="text-info hover:underline">Details →</Link>
      </footer>
    </article>
  );
}

function ActivityFeed({ items, note }: { items: Activity[]; note: string }) {
  const icon: Record<string, string> = { signal: "◆", decision: "⚖", trade: "⇄", event: "•" };
  return (
    <Panel title={<span className="flex items-center gap-2">Recent activity</span>}
      right={<Link href="/history/" className="btn">History</Link>}>
      {items.length === 0 ? <Empty>Nothing recorded yet.</Empty> : (
        <ol className="max-h-[360px] space-y-0.5 overflow-auto pr-1 text-[12.5px]">
          {items.map((a, i) => {
            const body = (
              <>
                <span className="num w-[7.5rem] shrink-0 text-mute">{fmtShort(a.at)}</span>
                <span className="w-9 shrink-0 text-mute">{a.runner === "xauusd" ? "XAU" : a.runner === "btcusdt" ? "BTC" : "A"}</span>
                <span className="w-4 shrink-0 text-center text-mute">{icon[a.type] ?? "•"}</span>
                <span className="min-w-0 flex-1 truncate text-slate-300">{a.text}</span>
                {a.type === "signal" && <PhaseTag status={a.phase === "warmup" ? "warmup" : "forward"} compact />}
                <KindTag kind={a.kind as "observed" | "simulated" | "derived"} />
              </>
            );
            return (
              <li key={i}>
                {a.signal_id ? <Link href={`/signal/?runner=${a.runner}&id=${encodeURIComponent(a.signal_id)}`} className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-ink-850">{body}</Link>
                  : <div className="flex items-center gap-2 px-1.5 py-1">{body}</div>}
              </li>
            );
          })}
        </ol>
      )}
      <p className="mt-2 text-[11px] text-mute">Forward signals, risk decisions, simulated trades and runner events, newest first. {note}</p>
    </Panel>
  );
}

function AlertsPanel({ g }: { g: AlertGroups }) {
  const mk = (m: string) => (m === "xauusd" ? "XAU" : m === "btcusdt" ? "BTC" : m);
  const [open, setOpen] = useState<string | null>(null);
  const Section = ({ id, title, count, children, tone: t }: { id: string; title: string; count: number; children: React.ReactNode; tone: string }) => (
    <div className="border-t border-line/70 pt-2 first:border-0 first:pt-0">
      <button className="flex w-full items-center justify-between text-left" onClick={() => setOpen(open === id ? null : id)} aria-expanded={open === id}>
        <span className="text-[11px] font-semibold uppercase tracking-wider text-mute">{title}</span>
        <span className={`num rounded-full px-2 text-[11px] ${count ? t : "text-mute"}`}>{count}</span>
      </button>
      {open === id && <div className="mt-1.5">{children}</div>}
    </div>
  );
  return (
    <Panel title="Safety alerts">
      <div className={`mb-3 rounded-lg border px-3 py-2 text-[12.5px] ${g.active.length ? "border-down/40 bg-down/10 text-down" : "border-up/30 bg-up/10 text-up"}`}>
        {g.active.length ? (
          <ul className="space-y-1">{g.active.map((a, i) => <li key={i}><span className="mr-2 text-mute">{mk(a.market)}</span>{humanize(a.text)}</li>)}</ul>
        ) : "No active safety alerts: no STOP/KILL file, breaker trip, halt, stale data or integrity problem right now."}
      </div>
      <div className="space-y-2">
        <Section id="conn" title="Temporary connection issues" count={g.connection.length} tone="bg-warn/15 text-warn">
          <ul className="space-y-1 text-[12px]">{g.connection.map((c, i) => (
            <li key={i}><span className="mr-2 text-mute">{mk(c.market)}</span>{c.text}<div className="text-[11px] text-mute">{c.status}{c.last_at ? ` · last ${fmtShort(c.last_at)}` : ""}</div></li>))}</ul>
        </Section>
        <Section id="res" title="Resolved warnings" count={g.resolved.length} tone="bg-slate-500/20 text-slate-300">
          <ul className="space-y-1.5 text-[12px]">{g.resolved.map((r, i) => (
            <li key={i}><span className="mr-2 text-mute">{mk(r.market)}</span>{humanize(r.text)}
              <div className="text-[11px] text-mute">{humanize(r.resolution)}{r.evidence ? ` · ${humanize(r.evidence)}` : ""}</div></li>))}</ul>
        </Section>
        <Section id="sk" title="Past STOP / KILL events" count={g.stop_kill.length} tone="bg-slate-500/20 text-slate-300">
          <ul className="space-y-1 text-[12px]">{g.stop_kill.map((s, i) => (
            <li key={i}><span className="mr-2 text-mute">{mk(s.market)}</span>{s.text} at {fmtShort(s.at)}<div className="text-[11px] text-mute">{s.resolution}</div></li>))}</ul>
        </Section>
      </div>
      <p className="mt-3 text-[11px] text-mute">Window: last {g.window_hours ?? 24} h (STOP/KILL: 7 days). Click a group to expand. All entries come from the runners&apos; own records.</p>
    </Panel>
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
  if (!data) return <ErrorNote error={error} updatedAt={updatedAt} />;
  const markets = Object.values(data.markets);
  const panels = Object.fromEntries(data.strategies.map((s) => [s.id, s]));
  return (
    <div className="space-y-4">
      <ErrorNote error={error} updatedAt={updatedAt} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight text-slate-50">Overview</h1>
        <div className="flex items-center gap-3 text-[11px] text-mute">
          <span className="flex items-center gap-1"><KindTag kind="observed" /> venue data</span>
          <span className="flex items-center gap-1"><KindTag kind="simulated" /> shadow books</span>
          <span className="flex items-center gap-1"><KindTag kind="derived" /> computed</span>
          <span className="flex items-center gap-1"><KindTag kind="unavailable" /> not recorded</span>
          <span>· refreshed {updatedAt ? fmtTime(updatedAt.toISOString(), false) : dash}</span>
        </div>
      </div>
      <section className="panel">
        <header className="panel-h">
          <h2 className="panel-t flex items-center gap-2">Live decision status <KindTag kind="observed" /><KindTag kind="derived" /></h2>
          <span className="hidden text-[11px] text-mute sm:block">Why each strategy is or is not trading right now: recorded data and the frozen rules only. Not a forecast.</span>
        </header>
        <div className="grid grid-cols-1 gap-3 p-3 lg:grid-cols-2 2xl:grid-cols-3">
          {(data.decisions ?? []).map((d) => <DecisionCard key={d.id} d={d} s={panels[d.id]} />)}
        </div>
      </section>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2"><ActivityFeed items={data.activity ?? []} note={data.warmup_note} /></div>
        {data.alert_groups && <AlertsPanel g={data.alert_groups} />}
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {markets.map((m) => <MarketCard key={m.market} m={m} h={data.health[m.market]} />)}
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2"><AccountsTable accounts={data.accounts} /></div>
        <Panel title="Risk limits">
          <div className="grid grid-cols-1 gap-3">
            {Object.entries(data.risk).map(([k, r]) => <RiskCard key={k} name={k} r={r} />)}
          </div>
        </Panel>
      </div>
    </div>
  );
}
