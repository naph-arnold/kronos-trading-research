"use client";

import { useEffect, useMemo, useState } from "react";
import { usePoll } from "@/lib/api";
import { dash, fmtNum, fmtShort, marketLabel } from "@/lib/format";
import type { McCompareRow, McHist, McIndex, McReport, McVariant, Quantiles } from "@/lib/types";
import { Empty, ErrorNote, KindTag, Loading, Panel, Stat } from "@/components/ui";

const METHOD_LABEL: Record<string, string> = {
  iid: "Trade resampling (bootstrap)", trade_block: "Block bootstrap (consecutive trades)",
  calendar_block: "Calendar-period bootstrap", regime_stratified: "Regime-stratified sampling",
  regime_conditional: "Regime-conditional",
};
const POLICY_LABEL: Record<string, string> = {
  "fixed_0.5pct": "Fixed 0.5% (operational)", drawdown_step: "Adaptive: drawdown step (≤0.5%)",
  loss_streak: "Adaptive: losing-streak step (≤0.5%)", volatility_regime: "Adaptive: high-volatility step (≤0.5%)",
};
const PARTITION_LABEL: Record<string, string> = {
  historical_holdout: "Historical holdout (examined)", forward: "Forward test", development: "Development",
};
const FROZEN = ["orb_newyork", "donchian", "selective_a"];
const pct = (p: number | null | undefined, d = 1) => (p == null ? dash : `${(100 * p).toFixed(d)}%`);
const q = (x: Quantiles, k: "p5" | "p50" | "p95", d = 2) => (x ? fmtNum(x[k], d) : dash);
const paramText = (p: Record<string, string | number>) => Object.entries(p).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ");

function Histogram({ h, marks, unit, color = "bg-info/60" }: { h: McHist; marks?: Quantiles; unit: string; color?: string }) {
  const max = Math.max(...h.counts, 1);
  const lo = h.edges[0], hi = h.edges[h.edges.length - 1];
  const pos = (v: number) => `${((v - lo) / (hi - lo || 1)) * 100}%`;
  return (
    <div>
      <div className="relative flex h-36 items-end gap-px rounded bg-ink-950/60 px-1 pt-2" role="img"
        aria-label={`distribution ${unit}, p5 ${marks?.p5} p50 ${marks?.p50} p95 ${marks?.p95}`}>
        {h.counts.map((c, i) => <div key={i} className={`flex-1 rounded-t-sm ${color}`} style={{ height: `${(c / max) * 100}%` }}
          title={`${fmtNum(h.edges[i])} – ${fmtNum(h.edges[i + 1])} ${unit}: ${c}`} />)}
        {marks && (["p5", "p50", "p95"] as const).map((k) => (
          <div key={k} className={`absolute inset-y-0 w-px ${k === "p50" ? "bg-slate-100" : "bg-warn"}`} style={{ left: pos(marks[k]) }}>
            <span className="absolute -top-0.5 left-1 text-[9.5px] text-slate-300">{k}</span>
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[10.5px] text-mute"><span>{fmtNum(lo)} {unit}</span><span>{fmtNum(hi)} {unit}</span></div>
    </div>
  );
}

function CompareTable({ rows }: { rows: { label: string; a: McCompareRow; b: McCompareRow }[] }) {
  if (!rows.length) return <Empty>No comparison available.</Empty>;
  return (
    <div className="overflow-auto">
      <table className="tbl">
        <thead><tr><th>Variant</th><th className="text-right">Median return</th><th className="text-right">5th pct return</th>
          <th className="text-right">95th pct max DD</th><th className="text-right">P(DD ≥ 5%)</th><th className="text-right">P(daily ≥ 1.5%)</th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.label}><td className="text-slate-300">{r.label}</td>
            <td className="num text-right">{fmtNum(r.a.p50_return_pct)}% → <b>{fmtNum(r.b.p50_return_pct)}%</b></td>
            <td className="num text-right">{fmtNum(r.a.p5_return_pct)}% → <b>{fmtNum(r.b.p5_return_pct)}%</b></td>
            <td className="num text-right">{fmtNum(r.a.p95_max_dd_pct)}% → <b>{fmtNum(r.b.p95_max_dd_pct)}%</b></td>
            <td className="num text-right">{pct(r.a.p_dd_ge_5pct)} → <b>{pct(r.b.p_dd_ge_5pct)}</b></td>
            <td className="num text-right">{pct(r.a.p_daily_loss_crossed, 2)} → <b>{pct(r.b.p_daily_loss_crossed, 2)}</b></td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Provenance({ r }: { r: McReport }) {
  const p = r.dataset.provenance ?? {};
  const rows: [string, unknown][] = [["Source", p.source], ["File", p.file], ["File SHA-256", p.file_sha256], ["Dataset SHA-256", p.dataset_sha256],
    ["Research config SHA-256", p.config_sha256], ["Holdout span", Array.isArray(p.strategy_holdout_span) ? (p.strategy_holdout_span as string[]).map((x) => fmtShort(x)).join(" → ") : undefined],
    ["Book", (r.dataset.book as Record<string, unknown> | undefined)?.label], ["Cost model", (r.dataset.cost_model as Record<string, unknown> | undefined)?.status],
    ["Validation", r.dataset.validation?.status], ["Dataset fingerprint", r.dataset_fingerprint], ["Algorithm", r.algorithm],
    ["Seed · scenarios", r.config ? `${r.config.seed} · ${r.config.scenarios}` : undefined], ["Result SHA-256", r.result_sha256],
    ["Generated", r.created_at ? fmtShort(r.created_at) : undefined], ["Note", p.note]];
  return (
    <dl className="text-[12px]">
      {rows.filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => (
        <div className="kv" key={k}><dt>{k}</dt><dd className="break-all text-right">{String(v).length > 70 ? `${String(v).slice(0, 16)}…` : String(v)}</dd></div>
      ))}
      {typeof p.examined === "string" && <p className="mt-2 rounded border border-warn/40 bg-warn/10 px-2 py-1.5 text-[11.5px] text-warn">{p.examined}</p>}
    </dl>
  );
}

export default function MonteCarloPage() {
  const idx = usePoll<McIndex>("/api/montecarlo", 0);
  const [market, setMarket] = useState("xauusd");
  const [id, setId] = useState("");
  const [method, setMethod] = useState("iid");
  const [policy, setPolicy] = useState("fixed_0.5pct");
  const [cost, setCost] = useState("recorded");
  const rows = useMemo(() => (idx.data?.reports ?? []).filter((r) => r.market === market), [idx.data, market]);
  useEffect(() => {
    // default: the frozen forward-test candidate's historical dataset, then any complete report
    const preferred = rows.find((r) => FROZEN.includes(r.strategy) && r.status === "complete")
      ?? rows.find((r) => r.status === "complete") ?? rows[0];
    if (!rows.some((r) => r.id === id)) setId(preferred?.id ?? "");
  }, [rows, id]);
  const rep = usePoll<McReport>(id ? `/api/montecarlo/${id}` : null, 0);
  const r = rep.data && rep.data.strategy && id.startsWith(`${rep.data.market}__${rep.data.strategy}__`) ? rep.data : null;
  const main = (r?.variants ?? []).filter((v) => v.group === "main");
  const methods = [...new Set(main.map((v) => v.method))];
  const policies = [...new Set(main.map((v) => v.policy))];
  const costs = [...new Set(main.map((v) => v.cost))];
  useEffect(() => { if (methods.length && !methods.includes(method)) setMethod(methods[0]); }, [methods, method]);
  const v: McVariant | undefined = main.find((x) => x.method === method && x.policy === policy && x.cost === cost)
    ?? main.find((x) => x.method === method && x.policy === "fixed_0.5pct" && x.cost === "recorded");
  const ev = r?.evidence;

  if (idx.loading && !idx.data) return <Loading />;
  return (
    <div className="space-y-4">
      <ErrorNote error={idx.error ?? rep.error} updatedAt={idx.updatedAt} />
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-50">Monte Carlo risk validation</h1>
          <p className="max-w-3xl text-[12px] text-mute">Resampling of recorded, closed, net-of-cost trades. Research only: it never changes signals, runners, permissions or risk limits, and it cannot turn a failed validation into a pass. Reports are precomputed; this page never starts a simulation.</p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {["xauusd", "btcusdt"].map((m) => <button key={m} className={`btn ${market === m ? "btn-on" : ""}`} onClick={() => setMarket(m)}>{marketLabel(m)}</button>)}
        </div>
      </div>

      {!idx.data?.available ? <Empty>{idx.data?.note ?? "No reports."}</Empty> : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <select className="input min-w-[22rem]" value={id} onChange={(e) => setId(e.target.value)} aria-label="Strategy and dataset">
              {rows.map((x) => <option key={x.id} value={x.id}>{FROZEN.includes(x.strategy) ? "★ " : ""}{x.strategy} · {PARTITION_LABEL[x.partition] ?? x.partition} · {x.closed_trades ?? 0} trades · {x.status.replace(/_/g, " ")}</option>)}
            </select>
            {r?.status === "complete" && (
              <>
                <select className="input" value={method} onChange={(e) => setMethod(e.target.value)} aria-label="Sampling method">
                  {methods.map((m) => <option key={m} value={m}>{METHOD_LABEL[m] ?? m}</option>)}
                </select>
                <select className="input" value={policy} onChange={(e) => setPolicy(e.target.value)} aria-label="Position sizing">
                  {policies.map((p) => <option key={p} value={p}>{POLICY_LABEL[p] ?? p}</option>)}
                </select>
                <select className="input" value={cost} onChange={(e) => setCost(e.target.value)} aria-label="Cost scenario">
                  {costs.map((c) => <option key={c} value={c}>{c === "recorded" ? "Recorded costs" : `Stress costs (assumed)`}</option>)}
                </select>
              </>
            )}
          </div>

          {!r ? <div className="skeleton h-64" /> : r.status !== "complete" ? (
            <Panel title={<span className="flex items-center gap-2">{r.strategy} · {PARTITION_LABEL[r.partition] ?? r.partition}</span>}>
              <div className="rounded-xl border border-dashed border-line px-4 py-6 text-center">
                <div className="text-lg font-semibold text-slate-100">Insufficient trading data</div>
                <p className="mx-auto mt-1 max-w-2xl text-[12.5px] text-mute">{r.reason}</p>
                <p className="mt-2 text-[12px] text-slate-400">Closed trades: <span className="num">{r.evidence?.closed_trades ?? 0}</span>. No distribution is shown, and none is fabricated.</p>
              </div>
            </Panel>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
                <span className={`rounded-full border px-2.5 py-0.5 font-semibold ${r.partition === "forward" ? "border-sim/50 bg-sim/15 text-sim" : "border-warn/50 bg-warn/10 text-warn"}`}>{PARTITION_LABEL[r.partition] ?? r.partition}</span>
                <KindTag kind="simulated" /><span className="text-mute">resampled paths of recorded trades · {v?.scenarios?.toLocaleString("en-US")} seeded scenarios · seed {String(r.config?.seed)}</span>
              </div>

              <div className="grid grid-cols-2 gap-4 rounded-2xl border border-line bg-ink-900/80 p-4 sm:grid-cols-3 xl:grid-cols-6">
                <Stat label="Closed trades" value={ev?.closed_trades} sub={`${ev?.months_with_trades} months with trades`} />
                <Stat label="Effective sample" value={fmtNum(ev?.dependence?.n_eff, 1)} sub={`lag-1 autocorr ${fmtNum(ev?.dependence?.lag1_autocorrelation, 3)}`} />
                <Stat label="Mean R (net)" value={fmtNum(ev?.mean_r, 3)} sub={ev?.mean_r_ci95 ? `95% CI ${fmtNum(ev.mean_r_ci95[0], 3)} to ${fmtNum(ev.mean_r_ci95[1], 3)}` : undefined}
                  valueClass={(ev?.mean_r ?? 0) > 0 ? "text-up" : "text-down"} />
                <Stat label="Selection-adjusted CI" value={<span className="text-sm">{ev?.multiple_testing?.mean_r_ci_adjusted ? `${fmtNum(ev.multiple_testing.mean_r_ci_adjusted[0], 3)} to ${fmtNum(ev.multiple_testing.mean_r_ci_adjusted[1], 3)}` : dash}</span>}
                  sub={`Bonferroni over ${ev?.multiple_testing?.comparisons ?? 1} strategies`} />
                <Stat label="Win rate" value={`${fmtNum(ev?.win_rate_pct, 1)}%`} sub={`${ev?.dependence?.overlapping_trades ?? 0} overlapping trades`} />
                <Stat label="Without best 3 trades" value={`${fmtNum(ev?.concentration?.net_r_without_best_3 ?? null, 1)} R`} sub={`top-5 = ${pct(ev?.concentration?.top5_share_of_positive_r ?? null, 0)} of gains`} />
              </div>

              {v && (
                <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                  <Panel title={<span className="flex items-center gap-2">Net return after costs <KindTag kind="simulated" /></span>}
                    right={<span className="text-[11px] text-mute">{METHOD_LABEL[v.method]}{Object.keys(v.params).length ? ` (${paramText(v.params)})` : ""}</span>}>
                    <Histogram h={v.hist.net_return_pct} marks={v.net_return_pct} unit="%" />
                    <div className="mt-2 grid grid-cols-3 gap-2 text-center text-[12px]">
                      <div><div className="text-mute">5th pct</div><div className="num">{q(v.net_return_pct, "p5")}%</div></div>
                      <div><div className="text-mute">median</div><div className="num font-semibold">{q(v.net_return_pct, "p50")}% <span className="text-[10px] text-mute">±{fmtNum(v.mc_standard_error.net_return_pct?.p50, 3)}</span></div></div>
                      <div><div className="text-mute">95th pct</div><div className="num">{q(v.net_return_pct, "p95")}%</div></div>
                    </div>
                    <p className="mt-2 text-[11px] text-mute">Ending equity of the {fmtNum(Number((r.dataset.book as Record<string, unknown> | undefined)?.initial_equity), 0)} USD book: {q(v.ending_equity_usd, "p5")} / {q(v.ending_equity_usd, "p50")} / {q(v.ending_equity_usd, "p95")} (5th / median / 95th). ± = Monte Carlo standard error of the median (simulation noise only).</p>
                  </Panel>
                  <Panel title={<span className="flex items-center gap-2">Maximum drawdown <KindTag kind="simulated" /></span>}>
                    <Histogram h={v.hist.max_drawdown_pct} marks={v.max_drawdown_pct} unit="%" color="bg-down/60" />
                    <div className="mt-2 grid grid-cols-3 gap-2 text-center text-[12px]">
                      <div><div className="text-mute">5th pct</div><div className="num">{q(v.max_drawdown_pct, "p5")}%</div></div>
                      <div><div className="text-mute">median</div><div className="num font-semibold">{q(v.max_drawdown_pct, "p50")}%</div></div>
                      <div><div className="text-mute">95th pct</div><div className="num">{q(v.max_drawdown_pct, "p95")}% <span className="text-[10px] text-mute">±{fmtNum(v.mc_standard_error.max_drawdown_pct?.p95, 3)}</span></div></div>
                    </div>
                  </Panel>
                  <Panel title="Losing streaks and recovery">
                    <div className="flex h-28 items-end gap-1">
                      {v.hist.losing_streak.map((c, i) => {
                        const m = Math.max(...v.hist.losing_streak, 1);
                        return <div key={i} className="flex h-full flex-1 flex-col items-center justify-end" title={`${i} losses: ${c}`}>
                          <div className="w-full rounded-t-sm bg-warn/60" style={{ height: `${(c / m) * 100}%` }} /><span className="text-[9.5px] text-mute">{i}</span></div>;
                      })}
                    </div>
                    <p className="mt-1 text-center text-[10.5px] text-mute">Longest losing streak per path (trades)</p>
                    <dl className="mt-3 text-[12px]">
                      <div className="kv"><dt>Longest losing streak (median · 95th)</dt><dd>{q(v.longest_losing_streak, "p50", 0)} · {q(v.longest_losing_streak, "p95", 0)}</dd></div>
                      <div className="kv"><dt>Longest recovered drawdown, trades (median · 95th)</dt><dd>{q(v.longest_recovered_spell_trades, "p50", 0)} · {q(v.longest_recovered_spell_trades, "p95", 0)}</dd></div>
                      <div className="kv"><dt>Paths ending underwater (censored)</dt><dd>{pct(v.probabilities.ends_underwater_censored?.p)}</dd></div>
                      <div className="kv"><dt>Open spell at end, trades (median, censored paths)</dt><dd>{q(v.censored_spell_trades, "p50", 0)}</dd></div>
                      <div className="kv"><dt>Time underwater (median)</dt><dd>{q(v.time_underwater_pct, "p50", 1)}% of trades</dd></div>
                    </dl>
                  </Panel>
                  <Panel title="Risk-threshold probabilities (simulated)">
                    <table className="tbl">
                      <thead><tr><th>Event over the path</th><th className="text-right">Probability</th><th className="text-right">MC s.e.</th></tr></thead>
                      <tbody>
                        {Object.entries(v.probabilities).filter(([k]) => k !== "ends_underwater_censored").map(([k, x]) => (
                          <tr key={k}><td>{k === "daily_loss_crossed" ? "Any day loses ≥ 1.5% (simulated halt rule)" : k.startsWith("dd_ge_") ? `Max drawdown ≥ ${Number(k.slice(6)) * 100}%` : `Equity ≤ start − ${Number(k.slice(15)) * 100}%`}</td>
                            <td className="num text-right">{pct(x.p, 2)}</td><td className="num text-right text-mute">{pct(x.mc_se, 2)}</td></tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="mt-2 text-[11px] text-mute">Simulated threshold crossings on resampled paths. They are not the behaviour of the operational circuit breaker, which also acts on open positions, quotes and other books.</p>
                  </Panel>
                </div>
              )}

              <Panel title="Fixed vs adaptive sizing (research only, recorded costs)" right={<span className="text-[11px] text-mute">fixed → adaptive</span>}>
                <CompareTable rows={(r.comparisons?.fixed_vs_adaptive ?? []).filter((x) => x.method === method).map((x) => ({ label: POLICY_LABEL[x.policy] ?? x.policy, a: x.fixed, b: x.adaptive }))} />
                <p className="mt-2 text-[11px] text-mute">Adaptive policies only ever reduce risk below the 0.5% ceiling using information available before each trade; no martingale. The operational sizing policy is not changed by these results.</p>
              </Panel>
              <Panel title="Recorded vs stress execution costs (fixed 0.5%)" right={<span className="text-[11px] text-mute">recorded → stress</span>}>
                <CompareTable rows={(r.comparisons?.recorded_vs_adverse_costs ?? []).map((x) => ({ label: METHOD_LABEL[x.method] ?? x.method, a: x.recorded, b: x.adverse }))} />
                <p className="mt-2 text-[11px] text-warn">{String((r.config?.cost_scenarios?.stress as Record<string, unknown> | undefined)?.basis ?? "")}</p>
              </Panel>

              <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                <Panel title="Sensitivity to sampling method and block length">
                  <div className="overflow-auto"><table className="tbl">
                    <thead><tr><th>Method</th><th className="text-right">Median return</th><th className="text-right">5th pct</th><th className="text-right">95th pct DD</th><th className="text-right">P(DD ≥ 5%)</th></tr></thead>
                    <tbody>{(r.comparisons?.method_sensitivity ?? []).map((m, i) => (
                      <tr key={i}><td>{METHOD_LABEL[m.method] ?? m.method}{Object.keys(m.params).length ? <span className="text-mute"> ({paramText(m.params)})</span> : null}</td>
                        <td className="num text-right">{fmtNum(m.p50_return_pct)}%</td><td className="num text-right">{fmtNum(m.p5_return_pct)}%</td>
                        <td className="num text-right">{fmtNum(m.p95_max_dd_pct)}%</td><td className="num text-right">{pct(m.p_dd_ge_5pct)}</td></tr>))}</tbody>
                  </table></div>
                </Panel>
                <Panel title="Regime coverage and conditional outcomes">
                  {ev?.regime_coverage ? (
                    <>
                      <div className="mb-2 flex flex-wrap gap-1.5">{Object.entries(ev.regime_coverage).map(([k, n]) => <span key={k} className="rounded-full border border-line px-2 py-0.5 text-[11px]">{k} <b className="num">{n}</b></span>)}</div>
                      {(r.comparisons?.regime_conditional ?? []).length === 0 ? <Empty>No regime has enough trades for conditional sampling.</Empty> : (
                        <table className="tbl"><thead><tr><th>If every trade came from</th><th className="text-right">Median return</th><th className="text-right">5th pct</th><th className="text-right">95th pct DD</th></tr></thead>
                          <tbody>{r.comparisons?.regime_conditional.map((x) => <tr key={x.regime}><td>{x.regime}</td><td className="num text-right">{fmtNum(x.p50_return_pct)}%</td><td className="num text-right">{fmtNum(x.p5_return_pct)}%</td><td className="num text-right">{fmtNum(x.p95_max_dd_pct)}%</td></tr>)}</tbody></table>
                      )}
                      <p className="mt-2 text-[11px] text-mute">Labels: research rule (EMA200 trend × ATR% vs rolling median), verified to reproduce the source report&apos;s per-regime totals. Regimes with fewer than {String(r.config?.min_regime_trades ?? 10)} trades are not sampled on their own.</p>
                    </>
                  ) : <Empty>No verified regime labels for this dataset.</Empty>}
                </Panel>
              </div>

              <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                <Panel title="Data provenance"><Provenance r={r} /></Panel>
                <Panel title="Limitations and how to read this">
                  <ul className="list-disc space-y-1.5 pl-4 text-[12px] text-slate-300">{(r.limitations ?? []).map((l, i) => <li key={i}>{l}</li>)}</ul>
                  {r.observed_path_fixed && (
                    <p className="mt-3 text-[11.5px] text-mute">Recorded order at fixed 0.5%: return {fmtNum(Number(r.observed_path_fixed.net_return_pct))}%, max drawdown {fmtNum(Number(r.observed_path_fixed.max_drawdown_pct))}%, longest losing streak {String(r.observed_path_fixed.longest_losing_streak)}.</p>
                  )}
                </Panel>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
