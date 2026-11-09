export const dash = "—";

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return dash;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtMoney(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return dash;
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${v < 0 ? "−" : ""}$${s}`;
}

export function fmtSigned(v: number | null | undefined, digits = 2, suffix = ""): string {
  if (v === null || v === undefined) return dash;
  const s = Math.abs(v).toFixed(digits);
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${s}${suffix}`;
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? dash : `${v.toFixed(digits)}%`;
}

/** UTC timestamp, compact. All Kronos times are UTC. */
export function fmtTime(iso: string | null | undefined, withDate = true): string {
  if (!iso) return dash;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  const t = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
  return withDate ? `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${t}` : t;
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return dash;
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

export function ageFrom(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (now - t) / 1000;
}

export function tone(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "text-slate-300";
  return v > 0 ? "text-up" : "text-down";
}
