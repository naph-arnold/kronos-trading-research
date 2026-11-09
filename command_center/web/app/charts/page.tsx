"use client";

import dynamic from "next/dynamic";
import { useMemo, useState } from "react";
import { usePoll } from "@/lib/api";
import Link from "next/link";
import { dash, fmtNum, fmtTime } from "@/lib/format";
import type { ChartData, ChartSignal, SignalRow } from "@/lib/types";
import type { Layers } from "@/components/CandleChart";
import { STRATEGY_COLOR } from "@/components/CandleChart";
import { Empty, ErrorNote, KindTag, Panel, StateBadge } from "@/components/ui";

const CandleChart = dynamic(() => import("@/components/CandleChart"), { ssr: false });

const TFS = [["4h", 14400], ["1h", 3600], ["15m", 900], ["5m", 300], ["1m", 60]] as const;
const MARKETS = [["xauusd", "XAUUSD"], ["btcusdt", "BTC/USDT"]] as const;
const STRATS: Record<string, { id: string; name: string }[]> = {
  xauusd: [{ id: "xau_orb", name: "NY Opening Range Breakout" }, { id: "xau_selective_a", name: "Selective Strategy A" }],
  btcusdt: [{ id: "btc_donchian", name: "Donchian Breakout" }],
};
type Avail = Record<string, { available: boolean; sources: { kind: string; base: string; from?: string; to?: string }[] }>;

const LAYER_LABELS: [keyof Layers, string][] = [
  ["volume", "Volume"], ["ema", "EMA 20/50"], ["signals", "Signals"], ["controls", "Baselines & controls"],
  ["warmup", "Warm-up signals"], ["trades", "Sim. entries/exits"], ["levels", "SL / TP"], ["ranges", "Opening ranges"],
  ["structure", "Swing structure"],
];

export default function ChartsPage() {
  const [market, setMarket] = useState<string>("xauusd");
  const [tf, setTf] = useState<string>("15m");
  const [limit, setLimit] = useState(500);
  const [until, setUntil] = useState<string | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [selected, setSelected] = useState<ChartSignal | null>(null);
  const [inspect, setInspect] = useState<string>("xau_orb");
  const [layers, setLayers] = useState<Layers>({ volume: true, ema: true, signals: true, controls: false, warmup: true,
    trades: true, levels: true, ranges: true, structure: false });

  const tfSeconds = TFS.find(([k]) => k === tf)?.[1] ?? 900;
  const qs = `tf=${tf}&limit=${limit}&controls=${layers.controls}&warmup=${layers.warmup}${until ? `&until=${encodeURIComponent(until)}` : ""}`;
  const chart = usePoll<ChartData>(`/api/chart/${market}?${qs}`, until ? 0 : 60000);
  const avail = usePoll<Avail>(`/api/availability/${market}`, 0);
  const sigs = usePoll<{ available: boolean; signals: SignalRow[]; note: string }>(`/api/strategies/${inspect}/signals?limit=300`, 60000);

  const atPick = useMemo(() => (chart.data && picked !== null ? chart.data.signals.filter((s) => s.time === picked) : []), [chart.data, picked]);

  const switchMarket = (m: string) => {
    setMarket(m); setUntil(null); setFocus(null); setSelected(null); setPicked(null);
    setInspect(STRATS[m][0].id);
  };
  const jumpTo = (row: SignalRow) => {
    const close = Math.floor(new Date(row.bar_close).getTime() / 1000);
    setUntil(new Date((close + tfSeconds * 120) * 1000).toISOString());
    setFocus(close - tfSeconds);
    setSelected({ time: close - tfSeconds, bar_close: row.bar_close, signal_id: row.signal_id, strategy: row.strategy,
      control: row.control, status: row.status, side: row.side, entry: row.entry, stop: row.stop, take_profit: row.take_profit,
      decisions: { approved: row.decisions.filter((d) => d.approved).length, rejected: row.decisions.filter((d) => !d.approved).length } });
  };
  const selectedRow = sigs.data?.signals.find((r) => r.signal_id === selected?.signal_id) ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-2 text-xl font-semibold tracking-tight text-slate-50">Charts</h1>
        {MARKETS.map(([k, label]) => (
          <button key={k} className={`btn ${market === k ? "btn-on" : ""}`} onClick={() => switchMarket(k)}>{label}</button>
        ))}
        <span className="mx-2 h-5 w-px bg-line" />
        {TFS.map(([k]) => {
          const a = avail.data?.[k];
          const off = a ? !a.available : false;
          return (
            <button key={k} disabled={off} title={off ? "No recorded data at this timeframe for this market" : undefined}
              className={`btn ${tf === k ? "btn-on" : ""} ${off ? "cursor-not-allowed opacity-40" : ""}`}
              onClick={() => { setTf(k); setFocus(null); }}>{k.toUpperCase()}</button>
          );
        })}
        <span className="mx-2 h-5 w-px bg-line" />
        <select className="btn" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          {[200, 500, 1000, 2000, 5000].map((n) => <option key={n} value={n}>{n} bars</option>)}
        </select>
        {until && (
          <button className="btn border-warn/50 text-warn" onClick={() => { setUntil(null); setFocus(null); }}>
            Viewing history to {fmtTime(until)} · back to latest
          </button>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {LAYER_LABELS.map(([k, label]) => (
          <button key={k} className={`btn ${layers[k] ? "btn-on" : ""}`} onClick={() => setLayers({ ...layers, [k]: !layers[k] })}>{label}</button>
        ))}
      </div>

      <ErrorNote error={chart.error} updatedAt={chart.updatedAt} />
      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[1fr_380px]">
        <section className="panel overflow-hidden">
          <div className="panel-h">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-semibold text-slate-200">{market === "xauusd" ? "XAUUSD" : "BTC/USDT"} · {tf.toUpperCase()}</span>
              <KindTag kind="observed" />
              {chart.data?.segments.some((s) => s.aggregated) && <KindTag kind="derived" />}
              {chart.data?.indicators.trend && <span className="text-mute">EMA trend: <span className={chart.data.indicators.trend === "up" ? "text-up" : "text-down"}>{chart.data.indicators.trend}</span></span>}
            </div>
            <span className="text-[11px] text-mute">{chart.data ? `${chart.data.candles.length} candles` : ""} · click a candle to inspect</span>
          </div>
          {chart.data && chart.data.candles.length > 0 ? (
            <CandleChart data={chart.data} layers={layers} tfSeconds={tfSeconds} selected={selected}
              onPick={(time) => setPicked(time)} focusTime={focus} />
          ) : (
            <div className="p-4">{chart.loading ? <span className="text-sm text-mute">Loading candles…</span> : <Empty>No recorded candles for this market and timeframe.</Empty>}</div>
          )}
          <div className="space-y-1 border-t border-line px-4 py-2.5 text-[11.5px]">
            {chart.data?.segments.map((s, i) => (
              <div key={i} className="text-mute">
                <span className="text-slate-300">{s.kind}</span> · {s.file} ({s.base_timeframe}{s.aggregated ? ` → ${tf}, aggregated` : ""}) · {fmtTime(s.from)} → {fmtTime(s.to)}
              </div>
            ))}
            {chart.data?.notes.map((n, i) => <div key={`n${i}`} className="text-warn">{n}</div>)}
            {chart.data?.has_volume && <div className="text-mute">Volume: {chart.data.volume_label}.</div>}
          </div>
        </section>

        <div className="space-y-4">
          <Panel title="Inspector">
            {selected ? (
              <SignalDetail row={selectedRow} sig={selected} onClear={() => setSelected(null)} />
            ) : atPick.length ? (
              <div className="space-y-2">
                {atPick.map((s) => (
                  <button key={s.signal_id} onClick={() => setSelected(s)} className="block w-full rounded-lg border border-line bg-ink-850 p-2 text-left text-xs hover:border-ink-600">
                    <span className="font-semibold" style={{ color: s.control ? "#8692a6" : STRATEGY_COLOR[s.strategy] }}>{s.strategy}</span> · {s.side} · {s.status}
                    <div className="num text-mute">entry {fmtNum(s.entry)} · stop {fmtNum(s.stop)} · target {fmtNum(s.take_profit)}</div>
                  </button>
                ))}
              </div>
            ) : (
              <p className="text-xs text-mute">Click a candle with a signal marker, or choose a recorded signal below.</p>
            )}
          </Panel>
          <Panel title="Signal history" right={
            <select className="btn" value={inspect} onChange={(e) => setInspect(e.target.value)}>
              {STRATS[market].map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>}>
            {!sigs.data ? <span className="text-xs text-mute">Loading…</span> : !sigs.data.available ? <Empty>{sigs.data.note}</Empty> : (
              <div className="max-h-[420px] overflow-auto">
                <table className="tbl">
                  <thead><tr><th>Bar close</th><th>Strategy</th><th>Side</th><th>Status</th></tr></thead>
                  <tbody>
                    {sigs.data.signals.map((r) => (
                      <tr key={r.signal_id} onClick={() => jumpTo(r)} className={`cursor-pointer hover:bg-ink-800 ${selected?.signal_id === r.signal_id ? "bg-ink-800" : ""}`}>
                        <td className="num whitespace-nowrap">{fmtTime(r.bar_close).slice(0, 16)}</td>
                        <td className={r.control ? "text-mute" : ""}>{r.strategy}</td>
                        <td className={r.side === "long" ? "text-up" : "text-down"}>{r.side}</td>
                        <td className="text-mute">{r.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="mt-2 text-[11px] text-mute">{sigs.data.note}</p>
              </div>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

function runnerOf(strategy: string): string {
  if (strategy === "selective_a") return "xau_selective_a";
  return ["donchian", "ema_trend", "kronos"].some((k) => strategy.startsWith(k) || strategy.startsWith(`random_${k}`)) ? "btcusdt" : "xauusd";
}

function SignalDetail({ row, sig, onClear }: { row: SignalRow | null; sig: ChartSignal; onClear: () => void }) {
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center justify-between">
        <span className="font-semibold" style={{ color: sig.control ? "#8692a6" : STRATEGY_COLOR[sig.strategy] }}>{sig.strategy}</span>
        <div className="flex gap-1.5">
          <Link className="btn btn-on" href={`/signal/?runner=${row ? runnerOf(row.strategy) : "xauusd"}&id=${encodeURIComponent(sig.signal_id)}`}>Full detail</Link>
          <button className="btn" onClick={onClear}>Clear</button>
        </div>
      </div>
      <dl>
        <div className="kv"><dt>Signal</dt><dd className="truncate">{sig.signal_id}</dd></div>
        <div className="kv"><dt>Bar close</dt><dd>{fmtTime(sig.bar_close)}</dd></div>
        <div className="kv"><dt>Side · status</dt><dd>{sig.side} · {sig.status}</dd></div>
        <div className="kv"><dt>Entry / stop / target</dt><dd>{fmtNum(sig.entry)} / {fmtNum(sig.stop)} / {fmtNum(sig.take_profit)}</dd></div>
        <div className="kv"><dt>Risk decisions</dt><dd>{sig.decisions.approved} approved · {sig.decisions.rejected} rejected</dd></div>
      </dl>
      {sig.status === "warmup" && <div className="rounded border border-warn/40 bg-warn/10 px-2 py-1 text-warn">Warm-up signal (before the forward boundary): not evidence, never traded.</div>}
      {row?.context && <pre className="max-h-40 overflow-auto rounded bg-ink-850 p-2 text-[10.5px] text-slate-300">{JSON.stringify(row.context, null, 1)}</pre>}
      {row && row.decisions.length > 0 && (
        <table className="tbl">
          <thead><tr><th>Book</th><th>Decision</th><th>Reasons</th></tr></thead>
          <tbody>{row.decisions.map((d) => (
            <tr key={d.book}><td className="num">{d.book}</td><td><StateBadge state={d.approved ? "allowed" : "blocked"} label={d.approved ? "approved" : "rejected"} /></td>
              <td className="text-mute">{d.reasons.join("; ") || dash}</td></tr>))}</tbody>
        </table>
      )}
      {row && row.trades.length > 0 && (
        <table className="tbl">
          <thead><tr><th>Book</th><th>P&L</th><th>R</th><th>Exit</th></tr></thead>
          <tbody>{row.trades.map((tr) => (
            <tr key={tr.book}><td className="num">{tr.book}</td><td className="num">{fmtNum(tr.pnl)}</td><td className="num">{fmtNum(tr.r)}</td><td>{tr.exit_reason}</td></tr>))}</tbody>
        </table>
      )}
    </div>
  );
}
