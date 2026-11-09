export const dash = "—";

/** Display time zone: UTC (default, how every Kronos record is stored) or the viewer's local zone. */
export type TzMode = "utc" | "local";
let tzMode: TzMode = "utc";
export function setTzMode(m: TzMode) {
  tzMode = m;
}
export function getTzMode(): TzMode {
  return tzMode;
}
export function localZoneName(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
  } catch {
    return "local";
  }
}
export function tzLabel(): string {
  return tzMode === "utc" ? "UTC" : localZoneName();
}

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

const p2 = (n: number) => String(n).padStart(2, "0");

/** ISO-style timestamp in the selected zone (24 h clock, unambiguous for an international audience). */
export function fmtTime(iso: string | number | Date | null | undefined, withDate = true): string {
  if (iso === null || iso === undefined || iso === "") return dash;
  const d = iso instanceof Date ? iso : new Date(typeof iso === "number" ? iso * 1000 : iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const utc = tzMode === "utc";
  const Y = utc ? d.getUTCFullYear() : d.getFullYear();
  const M = (utc ? d.getUTCMonth() : d.getMonth()) + 1;
  const D = utc ? d.getUTCDate() : d.getDate();
  const h = utc ? d.getUTCHours() : d.getHours();
  const m = utc ? d.getUTCMinutes() : d.getMinutes();
  const s = utc ? d.getUTCSeconds() : d.getSeconds();
  const t = `${p2(h)}:${p2(m)}:${p2(s)}`;
  return withDate ? `${Y}-${p2(M)}-${p2(D)} ${t}` : t;
}

export function fmtShort(iso: string | number | null | undefined): string {
  return fmtTime(iso).slice(0, 16);
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return dash;
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))} s`;
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

export function marketLabel(m: string): string {
  return m === "xauusd" ? "XAUUSD" : m === "btcusdt" ? "BTC/USDT" : m;
}
