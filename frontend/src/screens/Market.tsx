import { useEffect, useMemo, useState } from "react";
import { api, peek } from "../api";
import Sparkline from "../components/Sparkline";
import { fmtCompact, fmtPct, fmtPrice, useLocalStorage } from "../store";
import type { MarketRow } from "../types";
import { useServerMessages, wsClient } from "../ws";
import { navigate } from "../App";
import TradeTicket from "../components/TradeTicket";

type SortKey = "symbol" | "last" | "changePct" | "quoteVolume" | "high" | "low" | "vel1h" | "accel1h";

export default function Market() {
  const [rows, setRows] = useState<MarketRow[]>(() => peek<{ rows: MarketRow[] }>("/api/market")?.rows ?? []);
  const [minVol, setMinVol] = useLocalStorage("minVol", 10_000_000);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useLocalStorage<{ key: SortKey; dir: 1 | -1 }>("marketSort", { key: "quoteVolume", dir: -1 });
  const [ticket, setTicket] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState<string | null>(null);

  // ANALYZE: track the coin if needed, rescan now, then open it on the Analysis tab.
  const analyze = async (symbol: string) => {
    setAnalyzing(symbol);
    try { await api.scan(symbol); navigate("analysis", { symbol }); } catch (e) { console.error(e); }
    setAnalyzing(null);
  };

  // The backend refreshes its ticker cache every 30s; polling it locally is free.
  useEffect(() => {
    const load = () => api.market().then((r) => setRows(r.rows)).catch(console.error);
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, []);

  // Tracked pairs also get live ticker pushes between polls.
  const trackedSymbols = useMemo(() => rows.filter((r) => r.tracked).map((r) => r.symbol).join(","), [rows]);
  useEffect(() => {
    const releases = trackedSymbols.split(",").filter(Boolean).map((s) => wsClient.want(`ticker:${s}`));
    return () => releases.forEach((r) => r());
  }, [trackedSymbols]);
  useServerMessages((m) => {
    if (m.type !== "ticker") return;
    const t = m.ticker;
    setRows((prev) => prev.map((r) => {
      if (r.symbol !== t.symbol) return r;
      // Binance is the primary row; any other exchange lands in `others` for the divergence column.
      return t.exchange === "binance" ? { ...r, ...t } : { ...r, others: { ...r.others, [t.exchange]: t } };
    }));
  }, []);

  const divergenceBps = (r: MarketRow) => {
    const k = r.others?.kraken;
    return k && r.last ? (k.last / r.last - 1) * 10_000 : null;
  };

  const visible = useMemo(() => {
    const needle = filter.trim().toUpperCase();
    return rows
      .filter((r) => r.quoteVolume >= minVol && (!needle || r.symbol.includes(needle)))
      .sort((a, b) => {
        const av = a[sort.key] ?? -Infinity, bv = b[sort.key] ?? -Infinity;   // unknown momentum sorts last
        return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
      });
  }, [rows, minVol, filter, sort]);

  const header = (key: SortKey, label: string, right = true, title?: string) => (
    <th
      title={title}
      onClick={() => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : -1 }))}
      className={`px-2 py-1.5 font-normal cursor-pointer select-none hover:text-zinc-200 ${right ? "text-right" : "text-left"}`}
    >
      {label} {sort.key === key ? (sort.dir === 1 ? "▲" : "▼") : ""}
    </th>
  );

  return (
    <div className="p-3 flex flex-col gap-2 h-full">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter symbol…" className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 w-40 outline-none focus:border-zinc-600" />
        <label className="flex items-center gap-2 text-zinc-400">
          Min 24h volume
          <input type="range" min={0} max={9} step={0.25} value={Math.log10(Math.max(minVol, 1))} onChange={(e) => setMinVol(Math.round(10 ** Number(e.target.value)))} className="w-40" />
          <span className="num w-16">{fmtCompact(minVol)} USDT</span>
        </label>
        <span className="text-zinc-500">{visible.length} of {rows.length} USDT pairs</span>
        <span className="ml-auto text-zinc-500">24h figures are a rolling window ending now, not the UTC-day candle. 1h % and Accel need an hour or two of samples after a restart. Click a row to chart it.</span>
      </div>
      <div className="flex-1 min-h-0 overflow-auto rounded border border-zinc-800">
        <table className="w-full text-sm num">
          <thead className="sticky top-0 bg-zinc-950 text-zinc-500">
            <tr>
              {header("symbol", "Symbol", false)}
              {header("last", "Last")}
              {header("changePct", "24h %")}
              {header("quoteVolume", "24h Volume (USDT)")}
              {header("high", "24h High")}
              {header("low", "24h Low")}
              {header("vel1h", "1h %", true, "How fast price is moving right now: change over the last hour.")}
              {header("accel1h", "Accel", true, "Is it speeding up? This hour's change minus the previous hour's, in percentage points. Positive means momentum is building, negative means it is fading.")}
              <th className="px-2 py-1.5 font-normal text-right" title="Kraken last price minus Binance last price, in basis points. Tracked pairs listed on Kraken only.">Kraken Δ</th>
              <th className="px-2 py-1.5 font-normal text-left" title="Last 24 hourly closes. Only coins Wick tracks have local candles.">24h Trend</th>
              <th className="px-2 py-1.5 font-normal text-right"></th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr key={r.symbol} onClick={() => navigate("charts", { symbol: r.symbol })} className="border-t border-zinc-900 hover:bg-zinc-900 cursor-pointer">
                <td className="px-2 py-1 text-zinc-200">
                  {r.symbol}
                  {r.tracked && <span className="ml-1.5 inline-block w-1.5 h-1.5 rounded-full bg-sky-500 align-middle" title="tracked: live candles stored locally" />}
                </td>
                <td className="px-2 py-1 text-right">{fmtPrice(r.last)}</td>
                <td className={`px-2 py-1 text-right ${r.changePct >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtPct(r.changePct)}</td>
                <td className="px-2 py-1 text-right">{fmtCompact(r.quoteVolume)}</td>
                <td className="px-2 py-1 text-right text-zinc-400">{fmtPrice(r.high)}</td>
                <td className="px-2 py-1 text-right text-zinc-400">{fmtPrice(r.low)}</td>
                <td className={`px-2 py-1 text-right ${r.vel1h == null ? "text-zinc-600" : r.vel1h >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtPct(r.vel1h)}</td>
                <td className={`px-2 py-1 text-right ${r.accel1h == null ? "text-zinc-600" : r.accel1h >= 0 ? "text-emerald-400" : "text-red-400"}`}>{r.accel1h == null ? "–" : `${r.accel1h >= 0 ? "+" : ""}${r.accel1h.toFixed(2)}`}</td>
                <td className="px-2 py-1 text-right text-zinc-400">{divergenceBps(r) == null ? "–" : `${divergenceBps(r)! >= 0 ? "+" : ""}${divergenceBps(r)!.toFixed(1)}`}</td>
                <td className="px-2 py-1"><Sparkline values={r.sparkline} /></td>
                <td className="px-2 py-1 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => analyze(r.symbol)} disabled={analyzing === r.symbol} className="px-2 py-0.5 mr-1 rounded border border-zinc-700 text-zinc-400 hover:text-zinc-100 text-xs uppercase disabled:opacity-50">{analyzing === r.symbol ? "…" : "Analyze"}</button>
                  <button onClick={() => setTicket(r.symbol)} className="px-2 py-0.5 rounded bg-zinc-100 text-zinc-900 font-semibold text-xs uppercase">Trade</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="text-[13px] text-zinc-500 flex flex-wrap gap-4"><span><span className="inline-block w-1.5 h-1.5 rounded-full bg-sky-500 align-middle mr-1" /> Tracked: live candles stored locally.</span><span>Trade opens a ticket for any account. Analyze starts tracking the symbol in the Watching section of Analysis.</span></div>
      {ticket && <TradeTicket symbol={ticket} onClose={() => setTicket(null)} onDone={() => { setTicket(null); navigate("prop"); }} />}
    </div>
  );
}
