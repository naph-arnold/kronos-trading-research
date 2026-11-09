"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Same-origin JSON API served by the Python backend (read-only, GET only). */
export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, cache: "no-store", headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  updatedAt: Date | null;
  refresh: () => void;
}

/** Poll an endpoint; pauses while the tab is hidden so idle dashboards cost nothing. */
export function usePoll<T>(path: string | null, intervalMs = 15000): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const ctrl = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (!path) return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    try {
      const d = await getJSON<T>(path, c.signal);
      setData(d);
      setError(null);
      setUpdatedAt(new Date());
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!intervalMs) return () => ctrl.current?.abort();
    const id = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, intervalMs);
    const onVis = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      ctrl.current?.abort();
    };
  }, [load, intervalMs]);

  return { data, error, loading, updatedAt, refresh: load };
}
