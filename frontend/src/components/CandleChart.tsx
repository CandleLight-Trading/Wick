import { CandlestickSeries, createChart, createSeriesMarkers, HistogramSeries, type IChartApi, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type SeriesMarker, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api, peek } from "../api";
import { fmtPrice } from "../store";
import type { ChartStructure, Interval, WireCandle } from "../types";
import { useServerMessages, wsClient } from "../ws";
import Term from "./Term";
import { SHAPE_PLAIN } from "../glossary";

const UP = "#34d399";
const DOWN = "#f87171";
export type Guides = "off" | "structure" | "full";

function volumeBar(c: WireCandle) {
  return { time: c.time as UTCTimestamp, value: c.volume, color: c.close >= c.open ? UP + "66" : DOWN + "66" };
}
function candleBar(c: WireCandle) {
  return { time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close };
}

/** Plain sentences for what the structure engine found, in the order a trader would say them. */
export function structureSentences(s: ChartStructure): { k: string; text: string; value?: unknown }[] {
  const out: { k: string; text: string; value?: unknown }[] = [];
  if (!s.ok) return out;
  out.push({ k: s.structure === "uptrend" ? "chartUptrend" : s.structure === "downtrend" ? "chartDowntrend" : "chartRange",
    text: s.structure === "uptrend" ? "Higher highs and higher lows: buyers are accepting progressively higher prices." : s.structure === "downtrend" ? "Lower highs and lower lows: sellers are accepting progressively lower prices." : "No clean sequence of highs and lows: price is moving inside a range." });
  for (const e of s.events) {
    if (e.kind === "breakout") out.push({ k: "chartBreakout", text: `Price is above the level that contained the last ${60} bars${e.onVolume ? ", on elevated volume" : ", but volume has not expanded"}.`, value: e.onVolume });
    if (e.kind === "breakdown") out.push({ k: "chartBreakdown", text: `Price is below the level that held the last ${60} bars${e.onVolume ? ", on elevated volume" : ", but volume has not expanded"}.`, value: e.onVolume });
    if (e.kind === "compression") out.push({ k: "chartCompression", text: `The last 20 bars span only ${Math.round((e.ratio ?? 0) * 100)}% of the range before them: volatility has compressed.`, value: e.ratio });
    if (e.kind === "extension") out.push({ k: "chartExtension", text: `Price is ${Math.abs(e.atr ?? 0).toFixed(1)} ATR ${(e.atr ?? 0) > 0 ? "above" : "below"} its 20-bar mean: extended for this timeframe.`, value: e.atr });
    if (e.kind === "pullback") out.push({ k: "chartPullback", text: "Price has come back to its 20-bar mean inside a trend: a pullback, not a failure, so far." });
  }
  const sup = s.levels.filter((l) => l.kind === "support"), res = s.levels.filter((l) => l.kind === "resistance");
  if (sup.length) out.push({ k: "chartSupport", text: `Support near ${fmtPrice(sup[sup.length - 1].price)}${sup[sup.length - 1].touches > 1 ? ` (${sup[sup.length - 1].touches} touches)` : ""}.`, value: sup[sup.length - 1].touches });
  if (res.length) out.push({ k: "chartResistance", text: `Resistance near ${fmtPrice(res[0].price)}${res[0].touches > 1 ? ` (${res[0].touches} touches)` : ""}.`, value: res[0].touches });
  return out;
}

/**
 * One candlestick chart with a volume pane, kept live by the shared WebSocket.
 * The chart is created once; symbol/interval changes swap the data and subscription.
 * Guides draw what Wick's structure engine sees on the same candles: swing labels,
 * support and resistance lines, and (Full) the sentences that explain them.
 */
export default function CandleChart({ symbol, interval, guides = "off" }: { symbol: string; interval: Interval; guides?: Guides }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<UTCTimestamp> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const [last, setLast] = useState<WireCandle | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [structure, setStructure] = useState<ChartStructure | null>(null);
  const structureAt = useRef(0);

  // Create the chart once.
  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: { background: { color: "#09090b" }, textColor: "#a1a1aa", fontSize: 11 },
      grid: { vertLines: { color: "#18181b" }, horzLines: { color: "#18181b" } },
      rightPriceScale: { borderColor: "#27272a" },
      timeScale: { borderColor: "#27272a", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    });
    candles.current = c.addSeries(CandlestickSeries, {
      upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
    });
    // Second argument `1` puts the histogram in its own pane beneath the candles.
    volume.current = c.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "right" }, 1);
    c.panes()[1]?.setHeight(80);
    markers.current = createSeriesMarkers(candles.current as ISeriesApi<"Candlestick", UTCTimestamp>, []);
    chart.current = c;
    return () => { c.remove(); chart.current = null; };
  }, []);

  // Load history and subscribe whenever symbol/interval changes (or a backfill lands).
  useEffect(() => {
    let cancelled = false;
    const apply = (res: { candles: WireCandle[] }) => {
      if (cancelled || !candles.current || !volume.current) return;
      candles.current.setData(res.candles.map(candleBar));
      volume.current.setData(res.candles.map(volumeBar));
      const n = res.candles.length;
      chart.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 150), to: n + 5 });
      setLast(res.candles[n - 1] ?? null);
      setLoaded(true);
    };
    // Draw what we already have for this symbol at once; the fresh fetch replaces it a moment later.
    const cached = peek<{ candles: WireCandle[] }>(`/api/candles?symbol=${symbol}&interval=${interval}&limit=500`);
    if (cached) apply(cached); else setLoaded(false);
    api.candles(symbol, interval, 500).then(apply).catch(() => setLoaded(true));
    const release = wsClient.want(`kline:${symbol}:${interval}`);
    return () => { cancelled = true; release(); };
  }, [symbol, interval, reloadKey]);

  // Structure: fetched with the candles and again after each closed bar (at most every 30 s).
  const loadStructure = () => {
    structureAt.current = Date.now();
    api.structure(symbol, interval).then(setStructure).catch(() => {});
  };
  useEffect(() => {
    if (guides === "off") { setStructure(null); return; }
    setStructure(peek<ChartStructure>(`/api/structure?symbol=${symbol}&interval=${interval}`) ?? null);
    loadStructure();
  }, [symbol, interval, guides, reloadKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Draw the guides.
  useEffect(() => {
    const s = candles.current;
    if (!s) return;
    for (const l of lines.current) s.removePriceLine(l);
    lines.current = [];
    if (!structure || !structure.ok || guides === "off") { markers.current?.setMarkers([]); return; }
    const ms: SeriesMarker<UTCTimestamp>[] = structure.swings.map((p) => ({
      time: (p.time / 1000) as UTCTimestamp,
      position: p.kind === "high" ? "aboveBar" : "belowBar",
      color: p.kind === "high" ? (p.label === "HH" ? UP : "#a1a1aa") : (p.label === "HL" ? UP : "#a1a1aa"),
      shape: p.kind === "high" ? "arrowDown" : "arrowUp",
      text: p.label,
      size: 0.6,
    }));
    markers.current?.setMarkers(ms);
    for (const l of structure.levels) {
      lines.current.push(s.createPriceLine({ price: l.price, color: l.kind === "support" ? UP + "aa" : DOWN + "aa", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: l.kind === "support" ? `S ×${l.touches}` : `R ×${l.touches}` }));
    }
  }, [structure, guides]);

  useServerMessages((m) => {
    if (m.type === "kline" && m.symbol === symbol && m.interval === interval) {
      // update() replaces the bar with the same time or appends a newer one, which is exactly
      // the closed/unclosed semantics: the forming candle is overwritten until it closes.
      candles.current?.update(candleBar(m.candle));
      volume.current?.update(volumeBar(m.candle));
      setLast(m.candle);
      if (m.candle.closed && guides !== "off" && Date.now() - structureAt.current > 30_000) loadStructure();
    } else if (m.type === "backfilled" && m.symbol === symbol && m.interval === interval) {
      setReloadKey((k) => k + 1);
    }
  }, [symbol, interval, guides]);

  const up = last ? last.close >= last.open : true;
  const sentences = structure && guides === "full" ? structureSentences(structure) : [];
  return (
    <div className="relative h-full w-full rounded border border-zinc-800 overflow-hidden">
      <div ref={el} className="absolute inset-0" />
      <div className="absolute left-2 top-1.5 z-10 text-sm flex gap-3 pointer-events-none">
        <span className="font-semibold text-zinc-200">{symbol}</span>
        <span className="text-zinc-500">{interval}</span>
        {last && <span className={`num ${up ? "text-emerald-400" : "text-red-400"}`}>{fmtPrice(last.close)}</span>}
        {last && !last.closed && <span className="text-zinc-500">forming</span>}
        {!loaded && <span className="text-zinc-500">loading…</span>}
        {guides !== "off" && structure?.ok && <span className="text-zinc-500 uppercase tracking-wide text-xs self-center">{structure.structure}</span>}
      </div>
      {guides === "full" && structure?.ok && (
        <div className="absolute left-2 bottom-[92px] z-10 max-w-[340px] rounded border border-zinc-800 bg-zinc-950/85 backdrop-blur-sm p-2.5 text-[13px] leading-snug space-y-1">
          {structure.shape && (
            <div className="text-zinc-200"><span className="text-zinc-500">3-day shape (1h) · </span><Term k="shapeName" value={structure.shape.label} detail={structure.shape.name}><span className="cursor-help">{structure.shape.name}</span></Term>{structure.playbook && <span className="text-zinc-400"> · {structure.playbook}{structure.riskCharacter ? ` (${structure.riskCharacter})` : ""}</span>}</div>
          )}
          {structure.moveAtr != null && structure.atrDailyPct != null && (
            <div className="text-zinc-400"><Term k="move" value={structure.moveAtr} ctx={{ symbol, atrPct: structure.atrDailyPct }}><span className="cursor-help">Today {structure.moveAtr >= 0 ? "+" : ""}{structure.moveAtr.toFixed(2)} ATR</span></Term>{structure.volMultiple != null && <> · <Term k="volume" value={structure.volMultiple >= 1.3} detail={`${structure.volMultiple.toFixed(1)}×`}><span className="cursor-help">volume {structure.volMultiple.toFixed(1)}× normal</span></Term></>}</div>
          )}
          {sentences.map((s, i) => <div key={i} className="text-zinc-300"><Term k={s.k} value={s.value}><span className="cursor-help">{s.text}</span></Term></div>)}
          <div className="text-zinc-600 text-xs pt-0.5">Observation, not prediction. Hover any line for what it means.</div>
        </div>
      )}
      {guides === "full" && structure?.shape && !SHAPE_PLAIN[structure.shape.label] && null}
    </div>
  );
}
