"use client";

import { usePoll } from "@/lib/api";
import { dash, fmtMoney, fmtTime } from "@/lib/format";
import type { GrokRoom } from "@/lib/types";
import { Empty, ErrorNote, Loading, Panel, StateBadge } from "@/components/ui";

const ROLE_LABEL: Record<string, string> = {
  scout: "Scout", news: "News", chart: "Chart", kronos_analyst: "Kronos Analyst", skeptic: "Skeptic", coordinator: "Coordinator",
};

export default function GrokPage() {
  const { data, error, loading, updatedAt } = usePoll<GrokRoom>("/api/grok", 30000);
  if (loading && !data) return <Loading />;
  if (!data) return <ErrorNote error={error} updatedAt={updatedAt} />;
  const reviews = data.stores.flatMap((s) => s.summaries);
  return (
    <div className="space-y-4">
      <ErrorNote error={error} updatedAt={updatedAt} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight text-slate-50">Grok Decision Room</h1>
        <StateBadge state={data.status === "active" ? "running" : "prepared"} label={data.status} />
      </div>
      <div className="rounded-xl border border-info/30 bg-info/10 px-4 py-3 text-[13px] text-info">
        Advisory only. The six agents can never approve, size, block or modify a trade. Only the deterministic risk engine decides.
        {data.status !== "active" && " Paid API calls are disabled until the operator authorises activation."}
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title="Activation gate">
          <dl className="text-[12.5px]">
            <div className="kv"><dt>Config "activated"</dt><dd>{data.activated ? "true" : "false"}</dd></div>
            <div className="kv"><dt>GROK_AGENTS_ACTIVATED</dt><dd>{data.env_activation_flag ? "set" : "not set"}</dd></div>
            <div className="kv"><dt>XAI_API_KEY</dt><dd>{data.xai_key_present ? "present (not displayed)" : "not present"}</dd></div>
            <div className="kv"><dt>Provider</dt><dd className="text-[11px]">{data.provider}</dd></div>
            <div className="kv"><dt>Model (candidate)</dt><dd>{data.model ?? dash}</dd></div>
          </dl>
        </Panel>
        <Panel title="Cost controls">
          <dl className="text-[12.5px]">
            <div className="kv"><dt>Daily USD ceiling</dt><dd>{fmtMoney(data.daily_usd_budget)}</dd></div>
            <div className="kv"><dt>Daily call budget</dt><dd>{data.daily_call_budget ?? dash}</dd></div>
            <div className="kv"><dt>Price input / output</dt><dd>{data.pricing ? `$${data.pricing.input} / $${data.pricing.output} per M` : dash}</dd></div>
            <div className="kv"><dt>Spent today</dt><dd>{data.stores.length ? fmtMoney(data.stores.reduce((a, s) => a + (s.usd_today ?? 0), 0)) : "$0.00 (no calls)"}</dd></div>
          </dl>
        </Panel>
        <Panel title="Review trigger">
          <dl className="text-[12.5px]">
            <div className="kv"><dt>Mode</dt><dd>{data.trigger?.mode ?? dash}</dd></div>
            <div className="kv"><dt>Max signal age</dt><dd>{data.trigger ? `${data.trigger.max_signal_age_hours} h` : dash}</dd></div>
            <div className="kv"><dt>Max reviews per run</dt><dd>{data.trigger?.max_reviews_per_run ?? dash}</dd></div>
          </dl>
          {data.trigger?.note && <p className="mt-2 text-[11.5px] text-mute">{data.trigger.note}</p>}
        </Panel>
      </div>
      <Panel title="The six agents">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {data.roles.map((r) => (
            <div key={r.role} className="rounded-lg border border-line bg-ink-850 p-3">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-sm font-semibold text-slate-100">{ROLE_LABEL[r.role] ?? r.role}</span>
                <span className="text-[10px] uppercase tracking-wider text-mute">{data.status === "active" ? "ready" : "idle"}</span>
              </div>
              <p className="text-[12px] leading-relaxed text-slate-400">{r.brief}</p>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="Recorded reviews">
        {reviews.length === 0 ? (
          <Empty>No advisory reviews recorded. The agents are not activated, so no recommendation exists.</Empty>
        ) : (
          <table className="tbl">
            <thead><tr><th>Time</th><th>Subject</th><th>Runner</th><th>Recommendation</th></tr></thead>
            <tbody>{reviews.map((r) => (
              <tr key={r.subject + r.at}><td className="num">{fmtTime(r.at)}</td><td className="num">{r.subject}</td><td>{r.runner}</td><td>{r.recommendation}</td></tr>))}</tbody>
          </table>
        )}
        {data.stores.map((s) => (
          <div key={s.dir} className="mt-2 text-[11.5px] text-mute">
            Store {s.dir} · audit {s.audit ? (s.audit.ok ? <span className="text-up">chain verified ({s.audit.records} records)</span> : <span className="text-down">{s.audit.problems.join("; ")}</span>) : dash}
          </div>
        ))}
      </Panel>
    </div>
  );
}
