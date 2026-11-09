"use client";

import Link from "next/link";
import { usePoll } from "@/lib/api";
import { dash, fmtMoney, fmtNum, fmtPct, fmtSigned, fmtTime, tone } from "@/lib/format";
import type { StrategyPanel } from "@/lib/types";
import { Empty, ErrorNote, KindTag, Loading, Panel, PhaseTag, Stat, StateBadge } from "@/components/ui";

function StrategyCard({ s }: { s: StrategyPanel }) {
  const m = s.metrics;
  const prepared = s.lifecycle === "prepared";
  return (
    <section id={s.id} className="panel scroll-mt-20">
      <header className="panel-h">
        <div className="min-w-0">
          <h2 className="truncate text-[15px] font-semibold text-slate-100">{s.name}</h2>
          <div className="text-[11px] text-mute">strategy key <span className="num">{s.strategy_key}</span> · {s.market === "xauusd" ? "XAUUSD" : "BTC/USDT"}</div>
        </div>
        <StateBadge state={s.lifecycle} />
      </header>
      <div className="space-y-4 p-4">
        <p className={`rounded-lg px-3 py-2 text-[12.5px] ${prepared ? "border border-info/30 bg-info/10 text-info" : s.lifecycle === "unavailable" ? "border border-down/30 bg-down/10 text-down" : "bg-ink-850 text-slate-300"}`}>{s.lifecycle_detail}</p>
        {s.lifecycle !== "prepared" && <Link href={`/history/`} className="inline-flex items-center gap-1 text-[12px] text-info hover:underline">Search this strategy&apos;s signals, decisions and trades →</Link>}
        {prepared ? (
          <div className="space-y-3">
            <Empty>No signals, trades or performance: the runner has not been launched, so nothing has been recorded.</Empty>
            <dl className="text-[12.5px]">
              <div className="kv"><dt>Launch approved (config)</dt><dd>{s.launch_approved ? "yes" : "no"}</dd></div>
              {s.risk_limits && <div className="kv"><dt>Risk limits (frozen)</dt><dd>{(Number(s.risk_limits.risk_fraction) * 100).toFixed(1)}% / trade · {(Number(s.risk_limits.daily_loss_halt_fraction) * 100).toFixed(1)}% daily halt · {(Number(s.risk_limits.aggregate_risk_fraction) * 100).toFixed(1)}% aggregate</dd></div>}
            </dl>
          </div>
        ) : (
          <>
            <div className="flex items-center gap-2 text-[11px] text-mute"><KindTag kind="simulated" /> shadow-book results, headline book</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-4 xl:grid-cols-2 2xl:grid-cols-4">
              <Stat label="Forward signals" value={s.signals?.forward ?? 0}
                sub={<span className="inline-flex items-center gap-1"><PhaseTag status="warmup" compact /> {s.signals?.by_status?.warmup ?? 0} excluded</span>} />
              <Stat label="Sim. trades" value={m?.trades ?? 0} sub={m ? `${m.wins} won · ${m.losses} lost` : dash} />
              <Stat label="Net after est. costs" value={fmtMoney(m?.net_pnl)} valueClass={tone(m?.net_pnl)}
                sub={m ? `${fmtSigned(m.net_return_pct, 2, "%")} · costs ${fmtMoney(m.costs.spread_and_slippage + m.costs.commission + m.costs.swap)}` : dash} />
              <Stat label="Today / cumulative" value={<span className={tone(m?.pnl.today)}>{fmtSigned(m?.pnl.today)}</span>}
                sub={m ? `cumulative ${fmtSigned(m.pnl.cumulative)}` : dash} />
              <Stat label="Win rate" value={m?.win_rate_pct == null ? dash : fmtPct(m.win_rate_pct, 1)} sub={m?.trades ? undefined : "no closed trades yet"} />
              <Stat label="Profit factor" value={m?.profit_factor == null ? dash : fmtNum(m.profit_factor, 2)} sub={m?.mean_r == null ? undefined : `mean ${fmtNum(m.mean_r, 2)} R`} />
              <Stat label="Max drawdown" value={fmtPct(m?.max_drawdown_pct, 2)} />
              <Stat label="Risk decisions" value={m ? `${m.approved} / ${m.rejected}` : dash} sub="approved / rejected" />
            </div>
            <div className="text-[11.5px] text-mute">
              Headline book <span className="num text-slate-300">{s.primary_book ?? dash}</span> (largest equity, highest cost scenario).
              Last activity {fmtTime(s.last_activity)} · last heartbeat {fmtTime(s.last_heartbeat)}.
            </div>
            {s.books && s.books.length > 0 && (
              <div className="overflow-auto">
                <table className="tbl">
                  <thead><tr><th>Book</th><th className="text-right">Equity</th><th className="text-right">Cost</th><th className="text-right">Trades</th><th className="text-right">Net P&L</th><th className="text-right">Today</th></tr></thead>
                  <tbody>
                    {s.books.map((b) => (
                      <tr key={b.book_id} className={b.primary ? "bg-ink-850" : ""}>
                        <td className="num">{b.book_id}{b.primary && <span className="ml-1.5 text-[10px] text-info">headline</span>}</td>
                        <td className="num text-right">{fmtNum(b.pnl.equity)}</td>
                        <td className="num text-right text-mute">{b.commission_per_lot_round_trip}</td>
                        <td className="num text-right">{b.trades}</td>
                        <td className={`num text-right ${tone(b.net_pnl)}`}>{fmtNum(b.net_pnl)}</td>
                        <td className={`num text-right ${tone(b.pnl.today)}`}>{fmtSigned(b.pnl.today)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div>
              <div className="mb-1.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-mute">Rejected opportunities <KindTag kind="simulated" /></div>
              {m && Object.keys(m.rejection_reasons).length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {Object.entries(m.rejection_reasons).map(([k, v]) => (
                    <span key={k} className="rounded-full border border-line bg-ink-850 px-2 py-0.5 text-[11px]">{k.replace(/_/g, " ")} <span className="num text-warn">{v}</span></span>
                  ))}
                </div>
              )}
              {!s.rejections?.length ? (
                <p className="text-xs text-mute">No rejected opportunities recorded{s.signals?.forward ? "" : " (no forward signals yet)"}.</p>
              ) : (
                <div className="max-h-56 overflow-auto">
                  <table className="tbl">
                    <thead><tr><th>Time</th><th>Book</th><th>Reason</th></tr></thead>
                    <tbody>{s.rejections.map((r, i) => (
                      <tr key={i}><td className="num whitespace-nowrap">{fmtTime(r.at).slice(0, 16)}</td><td className="num">{r.book}</td><td>{r.reasons.join("; ")}</td></tr>))}</tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

export default function StrategiesPage() {
  const { data, error, loading, updatedAt } = usePoll<StrategyPanel[]>("/api/strategies", 20000);
  if (loading && !data) return <Loading />;
  return (
    <div className="space-y-4">
      <ErrorNote error={error} updatedAt={updatedAt} />
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight text-slate-50">Strategy monitoring</h1>
        <p className="text-[11.5px] text-mute">All figures are shadow simulations under the frozen pre-registered rules. Net figures include estimated spread, slippage, commission and swap.</p>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        {data?.map((s) => <StrategyCard key={s.id} s={s} />)}
      </div>
      <Panel title="Lifecycle states">
        <ul className="grid gap-2 text-[12.5px] sm:grid-cols-2 xl:grid-cols-5">
          <li><StateBadge state="prepared" /> <span className="text-mute">code ready, runner not launched</span></li>
          <li><StateBadge state="running" /> <span className="text-mute">heartbeats fresh (&lt; 3 min)</span></li>
          <li><StateBadge state="paused" /> <span className="text-mute">STOP file present</span></li>
          <li><StateBadge state="blocked" /> <span className="text-mute">KILL file or breaker trip</span></li>
          <li><StateBadge state="stopped" /> <span className="text-mute">no heartbeat for 5+ min</span></li>
        </ul>
      </Panel>
    </div>
  );
}
