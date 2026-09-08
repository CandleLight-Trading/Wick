import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import BaseRateStrip from "../components/BaseRateStrip";
import { fmtCompact, fmtNum, fmtPct, fmtPrice, fmtTime, useClickOutside, useLocalStorage } from "../store";
import type { ConditionEvent, ContextData, FuturesData, SymbolInfo, WireDepth } from "../types";
import { useServerMessages, wsClient } from "../ws";

function Card({ title, children, className = "" }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded border border-zinc-800 p-4 ${className}`}>
      <h3 className="text-[13px] uppercase tracking-wide text-zinc-500 mb-2">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="flex justify-between gap-3 py-1 text-sm" title={hint}>
      <span className="text-zinc-400">{label}</span>
      <span className="num text-zinc-100">{value}</span>
    </div>
  );
}

/** Type a partial symbol, Enter picks the best match. Untracked coins start tracking on pick. */
function SymbolSearch({ tracked, onPick }: { tracked: string[]; onPick: (sym: string) => void }) {
  const [all, setAll] = useState<SymbolInfo[]>([]);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);
  useEffect(() => { api.symbols().then((s) => setAll(s.filter((x) => x.quote === "USDT"))).catch(() => {}); }, []);
  const matches = useMemo(() => {
    const needle = q.trim().toUpperCase();
    if (!needle) return [];
    const ranked = all.filter((s) => s.symbol.includes(needle))
      .sort((a, b) => (a.symbol.startsWith(needle) ? 0 : 1) - (b.symbol.startsWith(needle) ? 0 : 1) || a.symbol.length - b.symbol.length);
    return ranked.slice(0, 8);
  }, [q, all]);
  const pick = (sym: string) => { setQ(""); setOpen(false); onPick(sym); };
  return (
    <div className="relative" ref={ref}>
      <input value={q} onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)}
        onKeyDown={(e) => { if (e.key === "Enter" && matches[0]) pick(matches[0].symbol); if (e.key === "Escape") setOpen(false); }}
        placeholder="Search symbol…" className="bg-zinc-900 border border-zinc-800 rounded px-3 py-1 text-[15px] w-48 outline-none focus:border-zinc-600" />
      {open && matches.length > 0 && (
        <ul className="absolute z-20 mt-1 w-60 rounded border border-zinc-800 bg-zinc-900 shadow-xl text-sm">
          {matches.map((s, i) => (
            <li key={s.symbol} onMouseDown={() => pick(s.symbol)} className={`px-3 py-1.5 cursor-pointer hover:bg-zinc-800 flex justify-between ${i === 0 ? "text-zinc-100" : "text-zinc-300"}`}>
              <span>{s.symbol}</span><span className="text-zinc-500 text-xs">{tracked.includes(s.symbol) ? "tracked" : "will start tracking"}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const fmtRate = (r: number | null | undefined) => (r == null ? "–" : `${r >= 0 ? "+" : ""}${(r * 100).toFixed(4)}%`);

/** Funding rate, mark vs spot basis, open interest and a 7-day funding strip.
 *  Funding is real positioning data: persistently positive means longs are crowded and
 *  paying to stay in. The strip is coloured by sign only; neither sign is "good". */
function FuturesBlock({ f, spot }: { f: FuturesData; spot: number }) {
  const hist = f.history;
  const maxAbs = Math.max(1e-9, ...hist.map((p) => Math.abs(p.rate)));
  const countdown = f.nextFundingTime ? Math.max(0, f.nextFundingTime - Date.now() / 1000) : null;
  const hh = countdown != null ? `${Math.floor(countdown / 3600)}h ${Math.floor((countdown % 3600) / 60)}m` : null;
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
      <div>
        <div className="flex items-baseline gap-2 mb-1">
          <span className="text-3xl font-semibold num text-zinc-50">{fmtRate(f.fundingRate)}</span>
          <span className="text-zinc-500">per 8h</span>
          <span className="text-zinc-400 num">{f.fundingAnnualizedPct != null ? `${f.fundingAnnualizedPct >= 0 ? "+" : ""}${f.fundingAnnualizedPct.toFixed(1)}% annualized` : ""}</span>
        </div>
        <Row label="Who pays" value={f.fundingRate == null ? "–" : f.fundingRate > 0 ? "longs pay shorts" : f.fundingRate < 0 ? "shorts pay longs" : "flat"} />
        <Row label="Next funding" value={hh ?? "accrues hourly (Kraken)"} />
        <Row label={`Mark price (${f.source})`} value={fmtPrice(f.markPrice)} />
        <Row label="Mark vs Binance spot" value={f.basisPct != null ? `${f.basisPct >= 0 ? "+" : ""}${(f.basisPct * 100).toFixed(1)} bps` : "–"} hint={f.source === "kraken-perp" ? "Kraken perps are USD-margined; this basis includes the USDT/USD rate" : undefined} />
        <Row label="Open interest" value={f.openInterest != null ? `${fmtCompact(f.openInterest)} ${f.symbol.replace("USDT", "")}` : "–"} />
        <Row label="Open interest, notional" value={f.openInterestNotional != null ? `$${fmtCompact(f.openInterestNotional)}` : "–"} />
        <div className="text-xs text-zinc-500 mt-1">spot {fmtPrice(spot)} · source {f.source}</div>
      </div>
      <div>
        <div className="text-zinc-500 mb-1">7-day funding history ({f.historySummary.n} points{f.source === "kraken-perp" ? ", hourly, shown per 8h" : ", every 8h"})</div>
        {hist.length === 0 ? <div className="text-zinc-500">Loads with the next poll.</div> : (
          <>
            <div className="flex items-center h-16 gap-px">
              {hist.map((p) => {
                const h = (Math.abs(p.rate) / maxAbs) * 50;
                return (
                  <div key={p.time} className="flex-1 h-full relative" title={`${fmtTime(p.time)}: ${fmtRate(p.rate)} per 8h`}>
                    <div className={`absolute left-0 right-0 ${p.rate >= 0 ? "bg-sky-400/80" : "bg-amber-400/80"}`} style={p.rate >= 0 ? { bottom: "50%", height: `${h}%` } : { top: "50%", height: `${h}%` }} />
                  </div>
                );
              })}
            </div>
            <div className="flex justify-between text-xs text-zinc-500"><span>{fmtTime(hist[0].time).slice(0, 10)}</span><span>above line = positive (longs pay)</span><span>now</span></div>
            {f.historySummary.n > 0 && (
              <div className="mt-1 text-zinc-400 num">
                mean {fmtRate(f.historySummary.mean)} · positive {((f.historySummary.positiveShare ?? 0) * 100).toFixed(0)}% of the time · range {fmtRate(f.historySummary.min)} to {fmtRate(f.historySummary.max)}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Screen 3. Shows what is happening and how unusual it is for this asset. It never says
 * what to do: no verdicts, no scores, no colours that mean "good" or "bad".
 */
export default function Context({ params }: { params: URLSearchParams }) {
  const [saved, setSaved] = useLocalStorage("contextSymbol", "BTCUSDT");
  const symbol = params.get("symbol") ?? saved;
  const [tracked, setTracked] = useState<string[]>([]);
  const [data, setData] = useState<ContextData | null>(null);
  const [events, setEvents] = useState<ConditionEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [book, setBook] = useState<WireDepth | null>(null);

  useEffect(() => { api.tracked().then(setTracked).catch(console.error); }, []);
  useEffect(() => { if (params.get("symbol")) setSaved(symbol); }, [symbol, params, setSaved]);

  useEffect(() => {
    let cancelled = false;
    setBook(null);
    setData(null); setError(null); setEvents([]);            // never show the previous coin's numbers under a new symbol
    const load = () => {
      api.context(symbol).then((d) => { if (!cancelled) { setData(d); setError(null); } })
        .catch((e) => {
          if (cancelled) return;
          const msg = String(e.message ?? e);
          // A freshly tracked coin has no closed candles yet: say so plainly and keep checking.
          if (/only \d+ closed|wait for backfill|not tracked/i.test(msg)) {
            setError(`Loading ${symbol}: Wick is downloading its history. This usually takes a minute or two.`);
            setTimeout(() => { if (!cancelled) load(); }, 10_000);
          } else setError(msg.replace(/^\d{3}\s*/, "").replace(/^\{"detail":"(.*)"\}$/, "$1"));
        });
      api.conditionLog(symbol).then((ev) => { if (!cancelled) setEvents(ev); }).catch(() => {});
    };
    load();
    const t = setInterval(load, 30_000);
    // Asking for depth makes the backend subscribe to <symbol>@depth20@100ms and start the
    // 500-level REST poll; releasing it stops both.
    const release = wsClient.want(`depth:${symbol}`);
    return () => { cancelled = true; clearInterval(t); release(); };
  }, [symbol]);

  useServerMessages((m) => { if (m.type === "depth" && m.depth.symbol === symbol) setBook(m.depth); }, [symbol]);

  const liveSpread = book && book.bids[0] && book.asks[0]
    ? ((book.asks[0][0] - book.bids[0][0]) / ((book.asks[0][0] + book.bids[0][0]) / 2)) * 10_000
    : null;

  // Live out-of-sample summary per condition from the condition log.
  const logSummary = useMemo(() => {
    const cost = data?.roundTripCost ?? 0.002;
    const by = new Map<string, { n: number; resolved: number; hits: number }>();
    for (const e of events) {
      const s = by.get(e.condition) ?? { n: 0, resolved: 0, hits: 0 };
      s.n += 1;
      if (e.ret24h != null) { s.resolved += 1; if (e.ret24h - cost > 0) s.hits += 1; }
      by.set(e.condition, s);
    }
    return [...by.entries()];
  }, [events, data]);

  return (
    <div className="p-3 flex flex-col gap-3 max-w-6xl">
      <div className="flex items-center gap-3">
        <select value={symbol} onChange={(e) => { setSaved(e.target.value); location.hash = `#/context?symbol=${e.target.value}`; }} className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[15px]">
          {[...new Set([symbol, ...tracked])].map((s) => <option key={s}>{s}</option>)}
        </select>
        <SymbolSearch tracked={tracked} onPick={async (sym) => {
          if (!tracked.includes(sym)) {
            try { setTracked(await api.setTracked([...tracked, sym])); } catch (e) { setError(String((e as Error).message)); return; }
          }
          setSaved(sym); location.hash = `#/context?symbol=${sym}`;
        }} />
        {data && <span className="text-[15px] num text-zinc-300">{fmtPrice(data.price)}</span>}
        {data && <span className="text-[13px] text-zinc-500">{data.history.bars1h.toLocaleString()} closed 1h bars stored, {fmtTime(data.history.from)} → {fmtTime(data.history.to)}</span>}
        {error && <span className="text-sm text-amber-400">{error}</span>}
      </div>

      {data && (
        <>
          {/* The single most important number on the screen. */}
          <section className="rounded border border-zinc-700 bg-zinc-900/60 p-4 flex items-baseline gap-6">
            <div>
              <div className="text-[13px] uppercase tracking-wide text-zinc-500">Volume vs 30-day average</div>
              <div className="text-5xl font-semibold num text-zinc-50">
                {data.volume.multiple != null ? `${data.volume.multiple.toFixed(1)}×` : "–"}
                <span className="text-[17px] font-normal text-zinc-400 ml-2">normal</span>
              </div>
            </div>
            <div className="text-sm text-zinc-400 num leading-relaxed">
              last 24h: {fmtCompact(data.volume.last24h)} {symbol.replace("USDT", "")}<br />
              avg per UTC day over {data.volume.days} closed days: {fmtCompact(data.volume.avg30d)}
            </div>
          </section>

          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            <Card title="Volatility (is today's move large for this asset?)">
              <Row label="Move since yesterday's UTC close" value={fmtPct(data.volatility.todayMovePct)} />
              <Row label="… in units of daily ATR(14)" value={data.volatility.todayMoveInAtr != null ? `${fmtNum(data.volatility.todayMoveInAtr)}×` : "–"} hint="Above 1 means today has already moved more than a typical day's range" />
              <Row label="ATR(14), daily bars" value={`${fmtPrice(data.volatility.atr14Daily)} (${fmtNum(data.volatility.atr14DailyPct)}%)`} />
              <Row label="ATR(14), hourly bars" value={fmtPrice(data.volatility.atr14Hourly)} />
              <Row label="Realized vol, 7d, annualized" value={data.volatility.realized7dAnnualized != null ? `${(data.volatility.realized7dAnnualized * 100).toFixed(0)}%` : "–"} />
              <Row label="Realized vol, 30d, annualized" value={data.volatility.realized30dAnnualized != null ? `${(data.volatility.realized30dAnnualized * 100).toFixed(0)}%` : "–"} />
              {data.impliedVol && (
                <>
                  <Row label={`Implied vol, 30d (${data.impliedVol.source})`} value={`${data.impliedVol.impliedVol30dPct.toFixed(1)}%`} />
                  <Row label="Variance premium (implied − realized)" value={data.impliedVol.variancePremiumPct != null ? `${data.impliedVol.variancePremiumPct >= 0 ? "+" : ""}${data.impliedVol.variancePremiumPct.toFixed(1)} pts` : "–"} hint="Positive is normal: options price more movement than has happened. Negative is unusual and worth noticing." />
                </>
              )}
              <Row label="Taker buy share, 24h" value={data.flow?.takerBuyRatio24 != null ? `${(data.flow.takerBuyRatio24 * 100).toFixed(0)}%` : "–"} hint="Share of volume bought by aggressors crossing the spread. Above 50% buyers were pressing." />
              <Row label="Taker buy share, 7d / 30d mean" value={`${data.flow?.takerBuyRatio7d != null ? (data.flow.takerBuyRatio7d * 100).toFixed(0) : "–"}% / ${data.flow?.takerBuyRatio30dMean != null ? (data.flow.takerBuyRatio30dMean * 100).toFixed(0) : "–"}%`} />
            </Card>

            <Card title="Price vs moving averages (1h bars)">
              {["20", "50", "200"].map((n) => (
                <Row key={n} label={`${n}-period MA ${data.ma[n].value != null ? fmtPrice(data.ma[n].value) : ""}`} value={fmtPct(data.ma[n].distancePct)} hint="Percentage distance of price from the average. Distance, not a crossover signal." />
              ))}
              <Row label="Correlation to BTC, 30d daily returns" value={data.correlationBtc30d != null ? fmtNum(data.correlationBtc30d) : "–"} hint="If everything you watch sits near 0.9 you effectively hold one position" />
            </Card>

            <Card title={`RSI(14) on 1h bars: ${data.rsi.value != null ? data.rsi.value.toFixed(1) : "–"}`}>
              <div className="flex items-end gap-0.5 h-16">
                {data.rsi.histogram.bins.map((b, i) => {
                  const max = Math.max(...data.rsi.histogram.bins.map((x) => x.count)) || 1;
                  return (
                    <div key={i} className="flex-1 h-full flex flex-col justify-end" title={`RSI ${b.lo}–${b.hi}: ${b.count} bars`}>
                      <div className={`w-full rounded-sm ${i === data.rsi.histogram.currentBin ? "bg-sky-400" : "bg-zinc-700"}`} style={{ height: `${(b.count / max) * 100}%` }} />
                    </div>
                  );
                })}
              </div>
              <div className="flex justify-between text-xs text-zinc-500 mt-1"><span>0</span><span>50</span><span>100</span></div>
              <div className="text-sm text-zinc-400 mt-1">
                Current value is at the <span className="text-zinc-100 num">{data.rsi.histogram.percentile?.toFixed(0)}th</span> percentile of this symbol's own {data.rsi.histogram.n.toLocaleString()} hourly readings.
              </div>
            </Card>

            <Card title="Order book" className="md:col-span-2 xl:col-span-3">
              {data.book || book ? (
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
                  <div>
                    <Row label="Spread (live, top of book)" value={liveSpread != null ? `${fmtNum(liveSpread, 2)} bps` : "…"} />
                    {book && book.bids[0] && <Row label="Best bid / ask (live)" value={`${fmtPrice(book.bids[0][0])} / ${fmtPrice(book.asks[0][0])}`} />}
                    {data.book && <Row label={`Spread (${data.book.source} snapshot, ${data.book.ageS.toFixed(0)}s old)`} value={`${fmtNum(data.book.spreadBps, 2)} bps`} />}
                    {data.book && <Row label={`Snapshot depth (${data.book.levels} levels/side)`} value={`±${fmtNum(data.book.coveragePct, 2)}% of mid`} />}
                    {!data.book && <div className="text-zinc-500 mt-1">Depth bands arrive with the next refresh (≤30s).</div>}
                    {data.slippage && (
                      <div className="mt-2">
                        <div className="text-zinc-500 mb-0.5">Slippage from mid if you cross the book now</div>
                        {data.slippage.rows.map((r) => (
                          <Row key={r.notional} label={`$${fmtCompact(r.notional)}${r.notional === data.accountSize ? " (your account)" : ""}`} value={`buy ${r.buyBps == null ? "–" : r.buyBps.toFixed(1)} / sell ${r.sellBps == null ? "–" : r.sellBps.toFixed(1)} bps${r.buyFilled < 1 || r.sellFilled < 1 ? " (partial)" : ""}`} />
                        ))}
                      </div>
                    )}
                  </div>
                  {(data.book?.bands ?? []).map((b) => {
                    const covered = data.book!.coveragePct >= b.widthPct;
                    return (
                      <div key={b.widthPct} className={covered ? "" : "opacity-50"} title={covered ? undefined : "The snapshot does not reach this far from mid; numbers understate real depth"}>
                        <div className="text-zinc-500 mb-1">within ±{b.widthPct}% of mid {covered ? "" : "(not fully covered)"}</div>
                        <Row label="bid notional" value={fmtCompact(b.bidNotional)} />
                        <Row label="ask notional" value={fmtCompact(b.askNotional)} />
                        <Row label="imbalance (bid−ask)/(bid+ask)" value={b.imbalance != null ? fmtNum(b.imbalance, 2) : "–"} />
                        {b.imbalance != null && (
                          <div className="h-1.5 mt-1 bg-zinc-800 rounded overflow-hidden flex">
                            <div className="bg-emerald-500/70" style={{ width: `${((b.imbalance + 1) / 2) * 100}%` }} />
                            <div className="bg-red-500/70 flex-1" />
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : <div className="text-sm text-zinc-500">Waiting for the first depth snapshot…</div>}
            </Card>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
            <Card title="Perpetual futures positioning" className="xl:col-span-2">
              {data.futures ? <FuturesBlock f={data.futures} spot={data.price} /> : (
                <div className="text-sm text-zinc-500">
                  {data.futuresStatus?.error ? `Futures data unavailable: ${data.futuresStatus.error}` : "Waiting for the first funding poll (≤60s)…"}
                </div>
              )}
            </Card>
            <Card title="Cross-exchange (same pair, other venues)">
              {Object.keys(data.crossExchange ?? {}).length === 0 ? (
                <div className="text-sm text-zinc-500">No other exchange lists this pair, or its feed is not connected.</div>
              ) : Object.entries(data.crossExchange).map(([name, x]) => (
                <div key={name} className="mb-2">
                  <Row label="Binance last" value={fmtPrice(data.price)} />
                  <Row label={`${name} last (${x.ageS.toFixed(0)}s old)`} value={fmtPrice(x.last)} />
                  <Row label="Divergence" value={x.divergenceBps == null ? "–" : `${x.divergenceBps >= 0 ? "+" : ""}${x.divergenceBps.toFixed(1)} bps`} hint="Kraken minus Binance, in basis points of the Binance price. Compare with the spread and the 0.20% fee before reading anything into it." />
                  <Row label={`${name} change since 00:00 UTC`} value={fmtPct(x.changePct)} hint="Kraken reports change since today's UTC open, not a rolling 24h window" />
                </div>
              ))}
            </Card>
          </div>

          <BaseRateStrip rates={data.baseRates} minSample={data.minSample} tests={data.testsEvaluated} cost={data.roundTripCost} />

          <Card title="Live condition log (out-of-sample: recorded before the outcome was known)">
            {events.length === 0 ? (
              <div className="text-sm text-zinc-500">Nothing logged yet. Onsets are recorded as 1h candles close; forward returns fill in over the following 24h.</div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 text-sm">
                <table className="w-full num">
                  <thead className="text-zinc-500"><tr><th className="text-left font-normal">condition</th><th className="text-right font-normal">onsets</th><th className="text-right font-normal">resolved @24h</th><th className="text-right font-normal">positive net</th></tr></thead>
                  <tbody>
                    {logSummary.map(([name, s]) => (
                      <tr key={name} className="border-t border-zinc-900">
                        <td className="py-1 text-zinc-300">{name}</td>
                        <td className="py-1 text-right">{s.n}</td>
                        <td className="py-1 text-right">{s.resolved}</td>
                        <td className="py-1 text-right">{s.resolved ? `${((s.hits / s.resolved) * 100).toFixed(0)}%` : "–"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="max-h-48 overflow-auto">
                  <table className="w-full num">
                    <thead className="text-zinc-500 sticky top-0 bg-zinc-950"><tr><th className="text-left font-normal">time (UTC)</th><th className="text-left font-normal">condition</th><th className="text-right font-normal">+1h</th><th className="text-right font-normal">+4h</th><th className="text-right font-normal">+24h</th></tr></thead>
                    <tbody>
                      {events.slice(0, 100).map((e) => (
                        <tr key={`${e.condition}-${e.time}`} className="border-t border-zinc-900 text-zinc-400">
                          <td className="py-1">{fmtTime(e.time).slice(0, 16)}</td>
                          <td className="py-1">{e.condition}</td>
                          {[e.ret1h, e.ret4h, e.ret24h].map((r, i) => <td key={i} className="py-1 text-right">{r == null ? "…" : fmtPct(r * 100)}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </Card>

          <p className="text-[13px] text-zinc-500 leading-snug">{data.caveats}</p>
        </>
      )}
      {!data && !error && <div className="text-[15px] text-zinc-500">Loading…</div>}
    </div>
  );
}
