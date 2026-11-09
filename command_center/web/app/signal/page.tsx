"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { usePoll } from "@/lib/api";
import { dash, fmtMoney, fmtNum, fmtSigned, fmtTime, tone } from "@/lib/format";
import type { ChartSignal, SignalDetail } from "@/lib/types";
import type { Layers } from "@/components/CandleChart";
import { Empty, ErrorNote, KindTag, Loading, Panel, Stat, StateBadge } from "@/components/ui";

const CandleChart = dynamic(() => import("@/components/CandleChart"), { ssr: false });
const TF: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400 };
const LAYERS: Layers = { volume: true, ema: true, signals: true, controls: false, warmup: true, trades: true,
  levels: true, ranges: true, structure: false };

function SignalView() {
  const params = useSearchParams();
  const runner = params.get("runner") ?? "xauusd";
  const id = params.get("id") ?? "";
  const { data, error, loading, updatedAt } = usePoll<SignalDetail>(id ? `/api/signal/${runner}/${encodeURIComponent(id)}` : null, 0);
  const [showRaw, setShowRaw] = useState(false);
  if (!id) return <Empty>No signal selected. Choose one in History or Charts.</Empty>;
  if (loading && !data) return <Loading rows={2} />;
  if (!data) return <ErrorNote error={error ?? "signal not found"} updatedAt={updatedAt} />;
  const close = Math.floor(new Date(data.bar_close).getTime() / 1000);
  const tf = TF[data.interval] ?? 900;
  const sel: ChartSignal = { time: close - tf, bar_close: data.bar_close, signal_id: data.signal_id, strategy: data.strategy,
    control: data.strategy.startsWith("random"), status: data.status, side: data.side, entry: data.entry, stop: data.stop,
    take_profit: data.take_profit, decisions: { approved: data.decisions.filter((d) => d.approved).length,
      rejected: data.decisions.filter((d) => !d.approved).length } };
  const risk = data.entry !== null && data.stop !== null ? Math.abs(data.entry - data.stop) : null;
  const reward = data.entry !== null && data.take_profit !== null ? Math.abs(data.take_profit - data.entry) : null;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-mute">{data.label} · signal record</div>
          <h1 className="num mt-0.5 break-all text-lg font-semibold text-slate-50">{data.signal_id}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px]">
            <span className="rounded-md bg-ink-800 px-2 py-0.5">{data.strategy}</span>
            <span className={data.side === "long" ? "text-up" : "text-down"}>{data.side}</span>
            <StateBadge state={data.status === "evaluated" ? "ok" : data.status === "warmup" ? "closed" : "paused"} label={data.status} />
            <span className="text-mute">bar close {fmtTime(data.bar_close)}</span>
          </div>
        </div>
        <Link href="/history/" className="btn">← History</Link>
      </div>
      {data.evidence_note && <div className="rounded-xl border border-warn/40 bg-warn/10 px-3 py-2 text-[12.5px] text-warn">{data.evidence_note}</div>}

      <div className="grid grid-cols-2 gap-4 rounded-2xl border border-line bg-ink-900/80 p-4 sm:grid-cols-3 xl:grid-cols-6">
        <Stat label="Entry" kind="derived" value={fmtNum(data.entry)} sub="strategy output" />
        <Stat label="Stop" value={fmtNum(data.stop)} sub={risk === null ? undefined : `risk ${fmtNum(risk)} / unit`} />
        <Stat label="Target" value={fmtNum(data.take_profit)} sub={risk && reward ? `${(reward / risk).toFixed(2)} R` : undefined} />
        <Stat label="Risk decisions" value={`${sel.decisions.approved} / ${sel.decisions.rejected}`} sub="approved / rejected" />
        <Stat label="Simulated trades" value={data.trades.length} sub={`${data.open_positions.length} open`} />
        <Stat label="Strategy version" value={<span className="text-sm">{data.strategy_version ?? dash}</span>} sub={data.params_hash ? `params ${data.params_hash}` : undefined} />
      </div>

      <section className="panel overflow-hidden">
        <div className="panel-h">
          <span className="panel-t">Market context · {data.interval.toUpperCase()}</span>
          <span className="flex items-center gap-2"><KindTag kind="observed" /><KindTag kind="derived" /></span>
        </div>
        {data.chart.candles.length ? (
          <CandleChart data={data.chart} layers={LAYERS} tfSeconds={tf} selected={sel} onPick={() => {}} focusTime={close - tf} height={440} />
        ) : <div className="p-4"><Empty>No recorded candles around this signal.</Empty></div>}
        <div className="space-y-0.5 border-t border-line px-4 py-2 text-[11.5px]">
          {data.chart.notes.map((n, i) => <div key={i} className="text-warn">{n}</div>)}
          {data.chart.segments.map((s, i) => <div key={`s${i}`} className="text-mute">{s.kind} · {s.file} · {fmtTime(s.from)} → {fmtTime(s.to)}</div>)}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title={<span className="flex items-center gap-2">Strategy context at the signal <KindTag kind="observed" /></span>}
          right={<button className="btn" onClick={() => setShowRaw(!showRaw)}>{showRaw ? "Table" : "Raw JSON"}</button>}>
          {!data.context ? <Empty>No context recorded with this signal.</Empty> : showRaw ? (
            <pre className="max-h-72 overflow-auto rounded-lg bg-ink-950 p-3 text-[11px] text-slate-300">{JSON.stringify(data.context, null, 2)}</pre>
          ) : (
            <dl className="text-[12.5px]">
              {Object.entries(data.context).map(([k, v]) => (
                <div key={k} className="kv"><dt>{k.replace(/_/g, " ")}</dt>
                  <dd>{typeof v === "number" ? fmtNum(v, 4) : Array.isArray(v) ? v.map((x) => (typeof x === "number" ? fmtNum(x) : String(x))).join(" – ") : String(v)}</dd></div>
              ))}
              <div className="kv"><dt>dataset</dt><dd>{data.dataset_id ?? dash}</dd></div>
              <div className="kv"><dt>generated at</dt><dd>{fmtTime(data.generated_at)}</dd></div>
            </dl>
          )}
        </Panel>
        <Panel title={<span className="flex items-center gap-2">Grok advisory review</span>}>
          {data.grok_reviews.length === 0 ? (
            <Empty>No advisory review recorded for this signal (Grok agents not activated).</Empty>
          ) : data.grok_reviews.map((r) => (
            <div key={r.at} className="mb-2 rounded-lg border border-line bg-ink-850 p-3 text-[12.5px]">
              <div className="flex justify-between"><span className="font-semibold">{r.recommendation}</span><span className="text-mute">{fmtTime(r.at)}</span></div>
              <pre className="mt-2 max-h-48 overflow-auto text-[11px] text-slate-400">{JSON.stringify(r.body, null, 2)}</pre>
            </div>
          ))}
          <p className="mt-2 text-[11px] text-mute">Advisory only: a review can never approve, size, block or modify a trade.</p>
        </Panel>
      </div>

      <Panel title={<span className="flex items-center gap-2">Risk-engine decisions <KindTag kind="simulated" /></span>}>
        {data.decisions.length === 0 ? (
          <Empty>{data.status === "warmup" ? "Warm-up signal: the risk engine never evaluated it." : "No risk decisions recorded for this signal yet."}</Empty>
        ) : (
          <div className="overflow-auto">
            <table className="tbl">
              <thead><tr><th>Book</th><th>Decision</th><th className="text-right">Size</th><th className="text-right">Planned risk</th><th className="text-right">% of equity</th><th>Reasons</th></tr></thead>
              <tbody>{data.decisions.map((d) => (
                <tr key={d.book}>
                  <td className="num">{d.book}</td>
                  <td><StateBadge state={d.approved ? "allowed" : "blocked"} label={d.approved ? "approved" : "rejected"} /></td>
                  <td className="num text-right">{fmtNum(d.size, 3)}</td>
                  <td className="num text-right">{fmtMoney(d.planned_risk)}</td>
                  <td className="num text-right">{d.planned_risk && (d.equity_at_decision ?? d.initial_equity) ? `${((100 * d.planned_risk) / (d.equity_at_decision ?? d.initial_equity ?? 1)).toFixed(3)}%` : dash}</td>
                  <td className="text-slate-300">{d.reasons.join("; ") || dash}</td>
                </tr>))}</tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title={<span className="flex items-center gap-2">Simulated execution & costs <KindTag kind="simulated" /></span>}>
        {data.trades.length === 0 && data.open_positions.length === 0 ? (
          <Empty>No simulated execution for this signal. Nothing is invented.</Empty>
        ) : (
          <div className="overflow-auto">
            <table className="tbl">
              <thead><tr><th>Book</th><th>Opened → closed</th><th className="text-right">Entry → exit</th><th className="text-right">Gross</th><th className="text-right">Spread/slip.</th><th className="text-right">Commission</th><th className="text-right">Swap</th><th className="text-right">Net</th><th className="text-right">R</th><th className="text-right">MFE / MAE</th><th>Exit</th></tr></thead>
              <tbody>
                {data.trades.map((t) => (
                  <tr key={t.book}>
                    <td className="num">{t.book}</td>
                    <td className="num whitespace-nowrap">{fmtTime(t.opened_at).slice(5, 16)} → {fmtTime(t.closed_at).slice(5, 16)}</td>
                    <td className="num text-right">{fmtNum(t.entry)} → {fmtNum(t.exit)}</td>
                    <td className={`num text-right ${tone(t.gross_pnl)}`}>{fmtNum(t.gross_pnl)}</td>
                    <td className="num text-right">{fmtNum(t.spread_slippage_cost)}</td>
                    <td className="num text-right">{fmtNum(t.commission)}</td>
                    <td className="num text-right">{fmtNum(t.swap)}</td>
                    <td className={`num text-right font-semibold ${tone(t.pnl)}`}>{fmtNum(t.pnl)}</td>
                    <td className={`num text-right ${tone(t.r)}`}>{fmtSigned(t.r)}</td>
                    <td className="num text-right">{fmtNum(t.mfe_r)} / {fmtNum(t.mae_r)}</td>
                    <td>{t.exit_reason ?? dash}</td>
                  </tr>
                ))}
                {data.open_positions.map((p) => (
                  <tr key={`o${p.book}`}><td className="num">{p.book}</td><td>open since {fmtTime(p.opened_at).slice(5, 16)}</td>
                    <td className="num text-right">{fmtNum(p.entry)}</td><td colSpan={8} className="text-mute">stop {fmtNum(p.stop)} · target {fmtNum(p.take_profit)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

export default function SignalPage() {
  return <Suspense fallback={<Loading rows={2} />}><SignalView /></Suspense>;
}
