import { CandlestickSeries, createChart, HistogramSeries, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { fmtPrice } from "../store";
import type { Interval, WireCandle } from "../types";
import { useServerMessages, wsClient } from "../ws";

const UP = "#34d399";
const DOWN = "#f87171";

function volumeBar(c: WireCandle) {
  return { time: c.time as UTCTimestamp, value: c.volume, color: c.close >= c.open ? UP + "66" : DOWN + "66" };
}
function candleBar(c: WireCandle) {
  return { time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close };
}

/**
 * One candlestick chart with a volume pane, kept live by the shared WebSocket.
 * The chart is created once; symbol/interval changes swap the data and subscription.
 */
export default function CandleChart({ symbol, interval }: { symbol: string; interval: Interval }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [last, setLast] = useState<WireCandle | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

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
    chart.current = c;
    return () => { c.remove(); chart.current = null; };
  }, []);

  // Load history and subscribe whenever symbol/interval changes (or a backfill lands).
  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    api.candles(symbol, interval, 500).then((res) => {
      if (cancelled || !candles.current || !volume.current) return;
      candles.current.setData(res.candles.map(candleBar));
      volume.current.setData(res.candles.map(volumeBar));
      const n = res.candles.length;
      chart.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 150), to: n + 5 });
      setLast(res.candles[n - 1] ?? null);
      setLoaded(true);
    }).catch(() => setLoaded(true));
    const release = wsClient.want(`kline:${symbol}:${interval}`);
    return () => { cancelled = true; release(); };
  }, [symbol, interval, reloadKey]);

  useServerMessages((m) => {
    if (m.type === "kline" && m.symbol === symbol && m.interval === interval) {
      // update() replaces the bar with the same time or appends a newer one, which is exactly
      // the closed/unclosed semantics: the forming candle is overwritten until it closes.
      candles.current?.update(candleBar(m.candle));
      volume.current?.update(volumeBar(m.candle));
      setLast(m.candle);
    } else if (m.type === "backfilled" && m.symbol === symbol && m.interval === interval) {
      setReloadKey((k) => k + 1);
    }
  }, [symbol, interval]);

  const up = last ? last.close >= last.open : true;
  return (
    <div className="relative h-full w-full rounded border border-zinc-800 overflow-hidden">
      <div ref={el} className="absolute inset-0" />
      <div className="absolute left-2 top-1.5 z-10 text-sm flex gap-3 pointer-events-none">
        <span className="font-semibold text-zinc-200">{symbol}</span>
        <span className="text-zinc-500">{interval}</span>
        {last && <span className={`num ${up ? "text-emerald-400" : "text-red-400"}`}>{fmtPrice(last.close)}</span>}
        {last && !last.closed && <span className="text-zinc-500">forming</span>}
        {!loaded && <span className="text-zinc-500">loading…</span>}
      </div>
    </div>
  );
}
