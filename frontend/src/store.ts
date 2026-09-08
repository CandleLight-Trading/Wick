import { useCallback, useEffect, useState } from "react";

/** useState that mirrors to localStorage. Keys are namespaced under "wick.". */
export function useLocalStorage<T>(key: string, initial: T): [T, (v: T | ((prev: T) => T)) => void] {
  const fullKey = `wick.${key}`;
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(fullKey);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const next = typeof v === "function" ? (v as (p: T) => T)(prev) : v;
        try { localStorage.setItem(fullKey, JSON.stringify(next)); } catch { /* private mode */ }
        return next;
      });
    },
    [fullKey],
  );
  return [value, set];
}

/** Close a popup when the user clicks anywhere outside `ref`. Clicks only, never hover. */
export function useClickOutside(ref: React.RefObject<HTMLElement | null>, onOutside: () => void, active = true) {
  useEffect(() => {
    if (!active) return;
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onOutside(); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [ref, onOutside, active]);
}

// ---- formatting helpers shared by all screens ----------------------------------
export const fmtPrice = (p: number | null | undefined) => {
  if (p == null) return "–";
  const digits = p >= 1000 ? 2 : p >= 1 ? 4 : p >= 0.01 ? 6 : 8;
  return p.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
export const fmtPct = (x: number | null | undefined, digits = 2) =>
  x == null ? "–" : `${x >= 0 ? "+" : ""}${x.toFixed(digits)}%`;
export const fmtCompact = (x: number | null | undefined) =>
  x == null ? "–" : Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 }).format(x);
export const fmtNum = (x: number | null | undefined, digits = 2) => (x == null ? "–" : x.toFixed(digits));
export const fmtTime = (s: number) => new Date(s * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
