"use client";

import { useEffect, useRef, useState } from "react";
import {
  ColorType, CrosshairMode, LineStyle, createChart,
  type IChartApi, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { ChartData, ChartSignal } from "@/lib/types";
import { fmtNum, fmtTime, getTzMode } from "@/lib/format";

export interface Layers {
  volume: boolean; ema: boolean; signals: boolean; controls: boolean; warmup: boolean;
  trades: boolean; levels: boolean; ranges: boolean; structure: boolean;
}

export const STRATEGY_COLOR: Record<string, string> = {
  orb_newyork: "#4f9cf9", donchian: "#f5a524", selective_a: "#b48cf2",
  h1_ema_cross: "#7dd3fc", ema_trend: "#fbbf24", kronos: "#f472b6",
};
const CONTROL_COLOR = "#5b6578";
const WARMUP_COLOR = "#7c8799";

const t = (s: number) => s as UTCTimestamp;

function tick(time: number, tickType: number): string {
  const d = new Date(time * 1000);
  const utc = getTzMode() === "utc";
  const p = (n: number) => String(n).padStart(2, "0");
  const H = utc ? d.getUTCHours() : d.getHours(), M = utc ? d.getUTCMinutes() : d.getMinutes();
  const day = utc ? d.getUTCDate() : d.getDate(), mon = (utc ? d.getUTCMonth() : d.getMonth()) + 1;
  if (tickType <= 2) return `${utc ? d.getUTCFullYear() : d.getFullYear()}-${p(mon)}-${p(day)}`; // year/month/day marks
  return `${p(H)}:${p(M)}`;
}

interface Hover { time: number; open: number; high: number; low: number; close: number; volume: number | null }

export default function CandleChart({ data, layers, tfSeconds, selected, onPick, focusTime, height = 560 }: {
  data: ChartData; layers: Layers; tfSeconds: number; selected: ChartSignal | null;
  onPick: (time: number) => void; focusTime: number | null; height?: number;
}) {
  const [hover, setHover] = useState<Hover | null>(null);
  const byTime = useRef(new Map<number, Hover>());
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick" | "Line" | "Histogram">[]>([]);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b0f15" }, textColor: "#8692a6", fontSize: 11,
        fontFamily: "JetBrains Mono, Cascadia Mono, Consolas, monospace" },
      grid: { vertLines: { color: "#141a24" }, horzLines: { color: "#141a24" } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#232c3a" },
      timeScale: { borderColor: "#232c3a", timeVisible: true, secondsVisible: false, rightOffset: 6,
        tickMarkFormatter: (time: Time, type: number) => tick(Number(time), type) },
      localization: { locale: "en-US", timeFormatter: (t: number) => fmtTime(t).slice(0, 16) },
    });
    c.subscribeClick((p) => { if (p.time !== undefined) pickRef.current(Number(p.time)); });
    c.subscribeCrosshairMove((p) => setHover(p.time !== undefined ? byTime.current.get(Number(p.time)) ?? null : null));
    chart.current = c;
    return () => { c.remove(); chart.current = null; series.current = []; candles.current = null; };
  }, []);

  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    for (const s of series.current) c.removeSeries(s);
    series.current = [];
    candles.current = null;
    byTime.current = new Map(data.candles.map((k) => [k.time, k]));
    if (!data.candles.length) return;
    const last = data.candles[data.candles.length - 1].time;
    const cs = c.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350",
      priceLineColor: "#4f9cf9",
    });
    cs.setData(data.candles.map((k) => ({ time: t(k.time), open: k.open, high: k.high, low: k.low, close: k.close })));
    candles.current = cs;
    series.current.push(cs);

    if (layers.volume && data.has_volume) {
      const vs = c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
      c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vs.setData(data.candles.filter((k) => k.volume !== null).map((k) => ({
        time: t(k.time), value: k.volume as number, color: k.close >= k.open ? "rgba(38,166,154,0.35)" : "rgba(239,83,80,0.35)" })));
      series.current.push(vs);
    }
    if (layers.ema) {
      for (const [key, color] of [["ema20", "#e2c26b"], ["ema50", "#6aa9ff"]] as const) {
        const ls = c.addLineSeries({ color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
        ls.setData(data.indicators[key].map((p) => ({ time: t(p.time), value: p.value })));
        series.current.push(ls);
      }
    }
    const segment = (from: number, to: number, price: number | null, color: string, style: LineStyle, width: 1 | 2 = 1) => {
      if (price === null) return;
      const end = to > from ? to : from + tfSeconds;
      const ls = c.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      ls.setData([{ time: t(from), value: price }, { time: t(end), value: price }]);
      series.current.push(ls);
    };
    if (layers.ranges) {
      for (const r of data.opening_ranges.slice(-40)) {
        segment(r.from, Math.max(r.to - tfSeconds, r.from), r.high, "#4f9cf9", LineStyle.Solid, 2);
        segment(r.from, Math.max(r.to - tfSeconds, r.from), r.low, "#4f9cf9", LineStyle.Solid, 2);
      }
    }
    if (layers.levels) {
      for (const tr of data.trades.slice(-40)) {
        segment(tr.entry_time, tr.exit_time, tr.stop, "#ef5350", LineStyle.Dashed);
        segment(tr.entry_time, tr.exit_time, tr.take_profit, "#26a69a", LineStyle.Dashed);
      }
      for (const p of data.positions) {
        segment(p.entry_time, last, p.stop, "#ef5350", LineStyle.Dashed, 2);
        segment(p.entry_time, last, p.take_profit, "#26a69a", LineStyle.Dashed, 2);
        segment(p.entry_time, last, p.entry, "#b48cf2", LineStyle.Dotted);
      }
    }

    const markers: SeriesMarker<Time>[] = [];
    if (layers.signals) {
      for (const s of data.signals) {
        if (s.control && !layers.controls) continue;
        if (s.status === "warmup" && !layers.warmup) continue;
        const long = s.side === "long";
        const warm = s.status === "warmup";
        // Warm-up (history before the forward boundary) is drawn grey and labelled; forward signals keep their colour.
        const color = s.control ? CONTROL_COLOR : warm ? WARMUP_COLOR : STRATEGY_COLOR[s.strategy] ?? "#cbd5e1";
        markers.push({ time: t(s.time), position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
          color, size: s.control ? 0.6 : warm ? 0.8 : 1.2,
          text: s.control ? "" : `${s.strategy.split("_")[0]} ${warm ? "warm-up" : "FORWARD"}` });
      }
    }
    if (layers.trades) {
      for (const tr of data.trades) {
        markers.push({ time: t(tr.entry_time), position: tr.side === "long" ? "belowBar" : "aboveBar", shape: "circle", color: "#b48cf2", size: 0.8, text: "entry" });
        markers.push({ time: t(tr.exit_time), position: tr.side === "long" ? "aboveBar" : "belowBar", shape: "square",
          color: (tr.pnl ?? 0) >= 0 ? "#26a69a" : "#ef5350", size: 0.8, text: tr.r === null ? "exit" : `${tr.r >= 0 ? "+" : ""}${tr.r.toFixed(2)}R` });
      }
    }
    if (layers.structure) {
      for (const p of data.structure) {
        markers.push({ time: t(p.time), position: p.kind === "high" ? "aboveBar" : "belowBar", shape: "circle", color: "#64748b", size: 0.3, text: p.label });
      }
    }
    for (const o of data.outliers ?? []) {
      markers.push({ time: t(o.time), position: "belowBar", shape: "circle", color: "#f5a524", size: 0.6, text: "off-market print" });
    }
    markers.sort((a, b) => Number(a.time) - Number(b.time));
    cs.setMarkers(markers);

    if (selected) {
      const lines: [number | null, string, string][] = [[selected.entry, "#b48cf2", "entry"], [selected.stop, "#ef5350", "stop"], [selected.take_profit, "#26a69a", "target"]];
      for (const [price, color, title] of lines) {
        if (price !== null) cs.createPriceLine({ price, color, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title });
      }
    }
  }, [data, layers, tfSeconds, selected]);

  useEffect(() => {
    const c = chart.current;
    if (!c || !data.candles.length) return;
    if (focusTime !== null) {
      const span = tfSeconds * 60;
      c.timeScale().setVisibleRange({ from: t(focusTime - span), to: t(focusTime + span / 2) });
    } else {
      c.timeScale().scrollToRealTime();
    }
  }, [focusTime, data, tfSeconds]);

  const last = data.candles[data.candles.length - 1];
  const h = hover ?? (last ? { ...last } : null);
  const up = h ? h.close >= h.open : true;
  return (
    <div className="relative w-full" style={{ height }}>
      <div ref={box} className="absolute inset-0" />
      {h && (
        <div className="pointer-events-none absolute left-3 top-2 z-10 flex flex-wrap gap-x-3 gap-y-0.5 rounded-md bg-ink-950/70 px-2 py-1 font-mono text-[11px] backdrop-blur">
          <span className="text-mute">{fmtTime(h.time).slice(0, 16)}</span>
          <span>O <span className={up ? "text-up" : "text-down"}>{fmtNum(h.open)}</span></span>
          <span>H <span className={up ? "text-up" : "text-down"}>{fmtNum(h.high)}</span></span>
          <span>L <span className={up ? "text-up" : "text-down"}>{fmtNum(h.low)}</span></span>
          <span>C <span className={up ? "text-up" : "text-down"}>{fmtNum(h.close)}</span></span>
          {h.volume !== null && <span className="text-mute">V {fmtNum(h.volume, 0)}</span>}
          {!hover && <span className="text-mute">(last)</span>}
        </div>
      )}
    </div>
  );
}
