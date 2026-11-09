"use client";

import { useEffect, useRef } from "react";
import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import type { EquityBook } from "@/lib/types";
import { fmtTime } from "@/lib/format";

const PALETTE = ["#4f9cf9", "#b48cf2", "#f5a524", "#26a69a", "#f472b6", "#7dd3fc", "#fbbf24", "#a3e635"];

/** Simulated equity (top) and drawdown % (bottom, derived) for the selected books. */
export default function EquityChart({ books, height = 320 }: { books: EquityBook[]; height?: number }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#8692a6", fontSize: 11 },
      grid: { vertLines: { color: "#141a24" }, horzLines: { color: "#141a24" } },
      rightPriceScale: { borderColor: "#232c3a", scaleMargins: { top: 0.06, bottom: 0.32 } },
      timeScale: { borderColor: "#232c3a", timeVisible: true },
      localization: { timeFormatter: (t: number) => fmtTime(t).slice(0, 16) },
    });
    books.forEach((b, i) => {
      const color = PALETTE[i % PALETTE.length];
      const eq = c.addLineSeries({ color, lineWidth: 2, title: b.book, priceLineVisible: false });
      eq.setData(b.equity.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
      const dd = c.addAreaSeries({ priceScaleId: "dd", lineColor: color, topColor: "rgba(239,83,80,0.0)",
        bottomColor: "rgba(239,83,80,0.25)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      dd.setData(b.drawdown_pct.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
    });
    c.priceScale("dd").applyOptions({ scaleMargins: { top: 0.74, bottom: 0 } });
    c.timeScale().fitContent();
    return () => c.remove();
  }, [books]);
  return <div ref={box} style={{ height }} className="w-full" />;
}
