import { useEffect, useState } from "react";
import { api, peek } from "../api";
import CandleChart from "../components/CandleChart";
import IntervalToggle from "../components/IntervalToggle";
import SymbolPicker from "../components/SymbolPicker";
import { useLocalStorage } from "../store";
import type { Interval, SymbolInfo } from "../types";
import { navigate } from "../App";
import TradeTicket from "../components/TradeTicket";

const DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"];

export default function Charts({ params }: { params: URLSearchParams }) {
  const [ticket, setTicket] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [all, setAll] = useState<SymbolInfo[]>(() => peek<SymbolInfo[]>("/api/symbols") ?? []);
  const [tracked, setTracked] = useState<string[] | null>(() => peek<string[]>("/api/tracked") ?? null);
  const [selected, setSelected] = useLocalStorage<string[]>("symbols", DEFAULT_SYMBOLS);
  const [interval, setInterval] = useLocalStorage<Interval>("interval", "1m");
  const [layout, setLayout] = useLocalStorage<1 | 4>("layout", 4);

  useEffect(() => {
    api.symbols().then(setAll).catch(console.error);
    api.tracked().then(setTracked).catch(console.error);
  }, []);

  // Deep link from the Market screen: put that symbol in the first slot.
  const linked = params.get("symbol");
  useEffect(() => {
    if (linked) setSelected((prev) => [linked, ...prev.filter((s) => s !== linked)].slice(0, 8));
  }, [linked, setSelected]);

  // Anything selected must be tracked by the backend, or there is no data to show.
  useEffect(() => {
    if (!tracked) return;
    const missing = selected.filter((s) => !tracked.includes(s));
    if (missing.length) api.setTracked([...tracked, ...missing]).then(setTracked).catch(console.error);
  }, [selected, tracked]);

  const shown = selected.slice(0, layout);

  return (
    <div className="h-full flex flex-col gap-2 p-2">
      <div className="flex flex-wrap items-center gap-3">
        <SymbolPicker all={all} selected={selected} onChange={setSelected} />
        <div className="ml-auto flex items-center gap-2">
          <IntervalToggle value={interval} onChange={setInterval} />
          <div className="inline-flex rounded border border-zinc-800 overflow-hidden">
            {([1, 4] as const).map((n) => (
              <button key={n} onClick={() => setLayout(n)} className={`px-2.5 py-1 text-sm ${layout === n ? "bg-zinc-700 text-white" : "text-zinc-400 hover:bg-zinc-800"}`}>
                {n === 1 ? "1 Chart" : "4 Charts"}
              </button>
            ))}
          </div>
          {shown[0] && (
            <>
              <button onClick={() => navigate("context", { symbol: shown[0] })} className="px-2.5 py-1 text-sm rounded border border-zinc-800 text-zinc-400 hover:bg-zinc-800">
                Context →
              </button>
              <button onClick={async () => { setAnalyzing(true); try { await api.scan(shown[0]); navigate("analysis", { symbol: shown[0] }); } finally { setAnalyzing(false); } }} disabled={analyzing} className="px-2.5 py-1 text-sm rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800 uppercase disabled:opacity-50">
                {analyzing ? "analyzing…" : `Analyze ${shown[0]}`}
              </button>
              <button onClick={() => setTicket(shown[0])} className="px-3 py-1 text-sm rounded bg-zinc-100 text-zinc-900 font-semibold uppercase">
                Trade {shown[0]}
              </button>
            </>
          )}
        </div>
      </div>
      {ticket && <TradeTicket symbol={ticket} onClose={() => setTicket(null)} onDone={() => { setTicket(null); navigate("prop"); }} />}
      <div className={`flex-1 min-h-0 grid gap-2 ${layout === 4 ? "grid-cols-2 grid-rows-2" : "grid-cols-1 grid-rows-1"}`}>
        {shown.map((sym) => <CandleChart key={sym} symbol={sym} interval={interval} />)}
        {shown.length === 0 && <div className="text-zinc-500 text-[15px] p-4">Pick a symbol above.</div>}
      </div>
      <div className="text-[13px] text-zinc-500">Times are UTC. The last candle is redrawn in place until it closes.</div>
    </div>
  );
}
