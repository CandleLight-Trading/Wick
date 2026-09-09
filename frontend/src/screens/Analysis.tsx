import { useEffect, useRef, useState } from "react";
import { api, peek } from "../api";
import { fmtClock, fmtCompact, fmtNum, fmtPct, fmtPrice, fmtTime, useClickOutside, useLocalStorage } from "../store";
import type { AnalysisPayload, Mover, PaperPayload, ShapeReport, SlippageTable } from "../types";
import { useServerMessages } from "../ws";
import { navigate } from "../App";
import TradeTicket from "../components/TradeTicket";
import Term from "../components/Term";
import { explainConcern, SHAPE_PLAIN } from "../glossary";

const STANCE = {
  long: "bg-emerald-500/15 text-emerald-300 border-emerald-700/50",
  short: "bg-red-500/15 text-red-300 border-red-700/50",
  flat: "bg-zinc-800 text-zinc-400 border-zinc-700",
};

function Stance({ s, small }: { s: string; small?: boolean }) {
  return <span className={`inline-block rounded border px-2 ${small ? "py-0 text-xs" : "py-1 text-[15px] font-semibold"} uppercase tracking-wide ${STANCE[s as keyof typeof STANCE] ?? STANCE.flat}`}>{s}</span>;
}

const num = (v: number | boolean | null | undefined, d = 2) => (typeof v === "number" ? v.toFixed(d) : "–");
/** The model sometimes writes markdown links inside plain-text fields; show the text only. */
const stripMd = (t: string) => t.replace(/\(\[([^\]]+)\]\((https?:[^)]+)\)\)/g, "").replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, "$1").replace(/\s+([.,;])/g, "$1").trim();
const ago = (s: number | null) => s == null ? "" : s < 3600 ? `${Math.max(1, Math.round(s / 60))}m ago` : s < 86400 ? `${(s / 3600).toFixed(1)}h ago` : `${Math.round(s / 86400)}d ago`;

/** The geometry of the last three days, and how that geometry resolved on this coin before. */
function ShapeBlock({ s }: { s: ShapeReport }) {
  const k = s.metrics;
  const h = s.horizons["24h"];
  const pct = (x?: number) => (x == null ? "–" : `${(x * 100).toFixed(0)}%`);
  const vr = typeof k.varianceRatio4 === "number" ? k.varianceRatio4 : null;
  return (
    <div className="rounded border border-zinc-800/80 bg-zinc-900/30 p-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-[13px] uppercase tracking-wide text-zinc-500">Shape</span>
        <span className="text-[15px] font-semibold text-zinc-100">{s.name}</span>
      </div>
      <div className="text-sm text-zinc-300 mt-0.5">{SHAPE_PLAIN[s.label] ?? s.description}</div>
      <div className="grid grid-cols-3 md:grid-cols-6 gap-x-3 gap-y-1 mt-1 text-[13px] num text-zinc-400">
        <span title="Net move over the last 24 / 72 bars in units of one typical day's range">move {num(k.move24, 1)} / {num(k.move72, 1)} days</span>
        <span title="Regression slope over 72 bars, % per day, and R² (1 = straight line)">slope {num(k.slope72PctPerDay, 1)}%/d · R² {num(k.r2_72)}</span>
        <span title="Kaufman efficiency: net move / total path. 1 = straight, 0 = went nowhere">efficiency {num(k.efficiency72)}</span>
        <span title="Variance ratio at 4 bars: >1 momentum, <1 mean reversion, 1 random walk">VR {vr == null ? "–" : vr.toFixed(2)} {vr != null ? (vr > 1.15 ? "momentum" : vr < 0.85 ? "reverting" : "random") : ""}</span>
        <span title="20-bar bandwidth vs its 30-day average. Below 0.5 = squeeze">bandwidth {num(k.bandwidthVsAvg)}× avg</span>
        <span title="Where price sits between the 3-day low (0) and high (1)">retrace {num(k.retrace)} {k.fellFirst ? "after drop" : "after rise"}</span>
      </div>
      <div className={`mt-1 text-[13px] num ${h.underpowered ? "text-zinc-500" : "text-zinc-300"}`}>
        On this coin, after this shape appeared ({s.nEpisodes} episodes): +24h net positive {pct(h.hitRateNet)} [{pct(h.ciLo)}–{pct(h.ciHi)}], median {h.medianNet == null ? "–" : fmtPct(h.medianNet * 100)}
        {h.underpowered && " · too few episodes to read"}{h.overlapping && " · overlapping"}
        {h.firstHalf && h.secondHalf && (
          <span className="text-zinc-500" title="Walk-forward check: first year of history vs second year. If the second is much worse, the pattern was fitted, not found."> · first year {pct(h.firstHalf.hitRateNet ?? undefined)} (n={h.firstHalf.n}) → second year {pct(h.secondHalf.hitRateNet ?? undefined)} (n={h.secondHalf.n})</span>
        )}
      </div>
    </div>
  );
}

/**
 * Two judges per mover, side by side. Rules: transparent checks. Model: research with links.
 * Both are logged and scored, and the scoreboard at the bottom is the only thing that
 * says whether either of them is worth listening to.
 */
function SlippageLine({ s, account, rulesPct }: { s: SlippageTable; account: number; rulesPct: number | null }) {
  const at = s.rows.find((r) => r.notional === account);
  const size = rulesPct != null ? (account * rulesPct) / 100 : null;
  return (
    <div className="text-[13px] num text-zinc-500">
      slippage from mid ({s.levels} levels):{" "}
      {s.rows.map((r) => (
        <span key={r.notional} className="mr-2" title={r.buyFilled < 1 || r.sellFilled < 1 ? "book too thin to fill this size in the snapshot" : ""}>
          ${fmtCompact(r.notional)} → buy {r.buyBps == null ? "–" : r.buyBps.toFixed(1)} / sell {r.sellBps == null ? "–" : r.sellBps.toFixed(1)} bps{r.buyFilled < 1 || r.sellFilled < 1 ? " (partial)" : ""}
        </span>
      ))}
      {at && size != null && <span className="text-zinc-400">· at the rules size (${fmtCompact(size)}) expect about {((at.buyBps ?? 0) + (at.sellBps ?? 0)).toFixed(1)} bps round trip on top of fees</span>}
    </div>
  );
}

function MoverDetails({ m, budget, account, onRun, running }: { m: Mover; budget: number; account: number; onRun: () => void; running: boolean }) {
  const f = m.features;
  const r = m.rules;
  const a = m.model;
  const agree = r && a && r.stance === a.stance;
  const cx = { symbol: m.symbol, atrPct: f.atrDailyPct, price: f.price };
  return (
    <section className="rounded border border-zinc-800 p-4 flex flex-col gap-2">
      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={() => navigate("charts", { symbol: m.symbol })} className="text-[17px] font-semibold hover:underline">{m.symbol}</button>
        <span className="num text-zinc-300">{fmtPrice(f.price)}</span>
        <span className={`num text-sm ${(f.change24hPct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtPct(f.change24hPct)} 24h</span>
        <Term k="unusualness" value={m.score} className="text-sm text-zinc-500 num"><span className="cursor-help">Unusualness {m.score.toFixed(2)}</span></Term>
        {agree != null && <span className={`text-xs uppercase tracking-wide px-1.5 rounded ${agree ? "bg-sky-900/40 text-sky-300" : "bg-zinc-800 text-zinc-500"}`}>{agree ? "judges agree" : "judges disagree"}</span>}
        <button onClick={() => navigate("context", { symbol: m.symbol })} className="ml-auto text-sm text-zinc-500 hover:text-zinc-200">Context →</button>
      </div>
      <div className="grid grid-cols-3 md:grid-cols-6 lg:grid-cols-11 gap-x-2 gap-y-1 text-[13px] num text-zinc-400">
        <Term k="move" value={f.moveAtr} ctx={cx}><span className="cursor-help">Move {f.moveAtr != null ? `${f.moveAtr.toFixed(2)} ATR` : "–"}</span></Term>
        <Term k="atr" value={f.atrDailyPct} ctx={cx}><span className="cursor-help">Typical day {f.atrDailyPct != null ? `${f.atrDailyPct.toFixed(1)}%` : "–"}</span></Term>
        <Term k="volume" value={f.volMultiple != null ? f.volMultiple >= 1.3 : undefined} detail={f.volMultiple != null ? `${f.volMultiple.toFixed(1)}×` : undefined}><span className="cursor-help">Volume {f.volMultiple != null ? `${f.volMultiple.toFixed(1)}×` : "–"}</span></Term>
        <Term k="rsi" value={f.rsi != null ? f.rsi > 25 && f.rsi < 75 : undefined} detail={f.rsi != null ? `RSI ${f.rsi.toFixed(0)}` : undefined}><span className="cursor-help">RSI {f.rsi != null ? f.rsi.toFixed(0) : "–"}</span></Term>
        <Term k="funding" value={f.fundingRate != null ? Math.abs(f.fundingRate) < 0.0005 : undefined} detail={f.fundingRate != null ? `${(f.fundingRate * 100).toFixed(4)}%/8h` : undefined}><span className="cursor-help">Funding {f.fundingRate != null ? `${(f.fundingRate * 100).toFixed(3)}%` : "–"}</span></Term>
        <Term k="oi" value={f.openInterestNotional}><span className="cursor-help">OI ${fmtCompact(f.openInterestNotional)}</span></Term>
        <Term k="divergence" value={f.divergenceBps}><span className="cursor-help">Kraken Δ {f.divergenceBps != null ? `${f.divergenceBps.toFixed(1)}bps` : "–"}</span></Term>
        <Term k="takers" value={f.takerBuyRatio24} className={f.takerBuyRatio24 == null ? "" : f.takerBuyRatio24 > 0.5 ? "text-emerald-400/90" : "text-red-400/90"}>
          <span className="cursor-help">Buyer urgency {f.takerBuyRatio24 != null ? `${(f.takerBuyRatio24 * 100).toFixed(0)}%` : "–"}{f.takerBuyRatio30d != null ? ` (${(f.takerBuyRatio30d * 100).toFixed(0)})` : ""}</span>
        </Term>
        <Term k="beta" value={f.betaBtc} ctx={cx}><span className="cursor-help">BTC beta {f.betaBtc != null ? f.betaBtc.toFixed(2) : "–"}</span></Term>
        <Term k="own" value={f.residual24hPct} ctx={cx}><span className="cursor-help">Own move {f.residual24hPct != null ? fmtPct(f.residual24hPct) : "–"}</span></Term>
        <Term k="sigma" value={f.ewmaDailyVolPct} ctx={cx}><span className="cursor-help">Vol/day {f.ewmaDailyVolPct != null ? `${f.ewmaDailyVolPct.toFixed(1)}%` : "–"}</span></Term>
      </div>
      {m.slippage && <SlippageLine s={m.slippage} account={account} rulesPct={r?.suggestedNotionalPct ?? null} />}
      {m.shape && <ShapeBlock s={m.shape} />}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="rounded bg-zinc-900/60 p-2">
          <div className="flex items-center gap-2 mb-1"><span className="text-[13px] uppercase tracking-wide text-zinc-500">Rules Say</span>{r && <Stance s={r.stance} />}</div>
          {r ? (
            <>
              <div className="text-sm text-zinc-300 mb-1">{r.summary}</div>
              <ul className="text-[13px] space-y-0.5">
                {r.checks.map((c) => (
                  <li key={c.name} className="flex gap-1.5"><span className={c.ok ? "text-emerald-400" : c.ok === false ? "text-red-400" : "text-zinc-500"}>{c.ok ? "✓" : c.ok === false ? "✗" : "·"}</span><span className="text-zinc-400">{c.name}</span><span className="text-zinc-500 num ml-auto">{c.detail}</span></li>
                ))}
              </ul>
              {r.stance !== "flat" && (
                <div className="text-[13px] text-zinc-400 num mt-1">
                  invalidation {fmtPrice(r.invalidation)} ({fmtNum(r.invalidationPct, 1)}% away) · horizon {r.horizonH}h<br />
                  size by stop: {fmtNum(r.suggestedNotionalPct, 0)}% of account (${fmtCompact((account * (r.suggestedNotionalPct ?? 0)) / 100)}) keeps a stop-out at {budget}% ·
                  size by vol: {fmtNum(r.volTargetNotionalPct, 0)}% (${fmtCompact((account * (r.volTargetNotionalPct ?? 0)) / 100)}) makes a 1σ day cost {budget}%
                </div>
              )}
            </>
          ) : <div className="text-sm text-zinc-500">not enough history</div>}
        </div>
        <div className="rounded bg-zinc-900/60 p-2">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[13px] uppercase tracking-wide text-zinc-500">Model Says</span>
            {a && <Stance s={a.stance} />}
            {a && <span className="text-xs text-zinc-500">{a.confidence} confidence · {a.trend.replace("_", " ")} · {a.horizonH}h</span>}
            <button onClick={onRun} disabled={running} className="ml-auto text-xs px-1.5 py-1 rounded border border-zinc-700 text-zinc-400 hover:text-zinc-100 disabled:opacity-40">{running ? "researching…" : a ? "Re-run" : "Research Now"}</button>
          </div>
          {a ? (
            <>
              <div className="text-sm text-zinc-200 mb-1">{a.summary}</div>
              <ul className="text-[13px] text-zinc-400 list-disc pl-4 space-y-0.5">
                {a.drivers.map((d, i) => <li key={i}>{d.text} {d.url && <a href={d.url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">source</a>}</li>)}
              </ul>
              {a.risks.length > 0 && <div className="text-[13px] text-amber-300/80 mt-1">risks: {a.risks.join("; ")}</div>}
              {a.stance !== "flat" && <div className="text-[13px] text-zinc-400 num mt-1">invalidation {fmtPrice(a.invalidation)} · numbers used: {a.numbers.join(", ")}</div>}
              <div className="text-xs text-zinc-500 mt-1">{fmtTime(a.time / 1000)} · {a.model}{a.usage?.total_tokens ? ` · ${a.usage.total_tokens} tokens` : ""}</div>
            </>
          ) : <div className="text-sm text-zinc-500">No model analysis yet. Top movers are researched automatically; anything else on demand.</div>}
        </div>
      </div>
    </section>
  );
}

const QUALITY = { strong: "text-emerald-300 border-emerald-800/60", mixed: "text-amber-300 border-amber-800/60", weak: "text-zinc-500 border-zinc-800" };

type CardActions = {
  onResearch: () => void; onDismiss: () => void; onBuild: () => void; onTrade: () => void; onClear: () => void; onQueue: () => void;
  onRun: () => void; running: boolean; researching: boolean;
};

/**
 * One card, three concepts, one dominant action.
 *   QUANT SIGNAL  free, updates every scan (rules judge)
 *   AI RESEARCH   only when you click; CURRENT / AGING / STALE with the reason
 *   WICK VERDICT  the synthesis: enter / wait / pass, or "research first"
 * Dominant action by state: unresearched -> RESEARCH; stale -> REFRESH RESEARCH;
 * researched ENTER -> BUILD TRADE; WAIT -> VIEW PLAN; PASS -> DISMISS SETUP.
 * "Trade Anyway" and "Clear Research" stay quiet and secondary.
 */
function MoverCard({ m, budget, account, a, focused }: { m: Mover; budget: number; account: number; a: CardActions; focused?: boolean }) {
  const [open, setOpen] = useState(!!focused);
  const s = m.setup;
  const f = m.features;
  const KEY: Record<string, string> = { Trend: "trend", Volume: "volume", Funding: "funding", RSI: "rsi", Move: "move", Flow: "flow", Shape: "shape" };
  const checks = s?.checks ?? m.rules?.checks ?? [];
  const chips = checks.map((c) => ({ name: c.name.split(" (")[0].replace("Shape does not veto", "Shape").replace("Move is meaningful", "Move").replace("Taker flow agrees", "Flow").replace("Volume confirms", "Volume").replace("Funding not crowded", "Funding").replace("RSI not at an extreme", "RSI"), ok: c.ok, detail: c.detail }));
  const concernCheck = checks.find((c) => s && c.name === s.concern);
  const held = m.held.filter((h) => h.state === "open");
  const pending = m.held.filter((h) => h.state !== "open");
  const r = s?.research;
  const rec = s?.recommendation;
  const rs = s?.researchState ?? "none";
  const researched = !!s && s.state === "researched" && rs !== "none";
  const headline = !s ? "WATCHING" : researched
    ? (rs === "stale" ? "RESEARCH STALE" : rec === "pass" ? "PASS" : rec === "wait" ? `WAIT · ${s.bias.toUpperCase()} BIAS` : `ENTER · ${s.bias.toUpperCase()}`)
    : s.quality === "weak" ? "WEAK" : `PROMISING ${s.bias === "neutral" ? "SETUP" : s.bias.toUpperCase()}`;
  const job = m.researchJob;
  const busy = !!job && (job.state === "queued" || job.state === "running");
  const playbook = s?.playbook ?? m.rules?.playbook ?? null;
  const character = s?.riskCharacter ?? m.rules?.riskCharacter ?? null;
  const primary = "px-3 py-1.5 rounded bg-zinc-100 text-zinc-900 text-sm font-semibold uppercase tracking-wide disabled:opacity-40";
  const quiet = "px-2 py-1.5 rounded border border-zinc-800 text-zinc-500 hover:text-zinc-200 text-sm";
  const stale1 = s?.staleReasons?.[0];
  return (
    <section id={`mover-${m.symbol}`} className={`rounded border p-4 flex flex-col gap-2 ${focused ? "border-sky-700 bg-sky-950/10" : "border-zinc-800"}`}>
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={() => navigate("charts", { symbol: m.symbol })} className="text-[17px] font-semibold hover:underline">{m.symbol}</button>
        <span className="num text-zinc-300">{fmtPrice(f.price)}</span>
        <span className={`num text-sm ${(f.change24hPct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtPct(f.change24hPct)} 24h</span>
        {s && <span className="text-sm text-zinc-500" title={`Added to Analysis ${fmtTime(s.detectedAt)}`}>Added {ago(Date.now() / 1000 - s.detectedAt)}</span>}
        {s && <Term k="quality" value={s.quality}><span className={`text-xs uppercase tracking-wide px-1.5 py-1 rounded border cursor-help ${QUALITY[s.quality]}`}>{s.quality} setup</span></Term>}
        <span className={`text-[15px] font-semibold tracking-wide ${rs === "stale" ? "text-amber-300" : "text-zinc-100"}`}>{headline}</span>
        {playbook && <span className="text-sm text-zinc-300" title={m.rules?.summary ?? ""}>{playbook}</span>}
        {character && <span className={`text-xs uppercase tracking-wide px-1.5 py-1 rounded border ${character === "aggressive" ? "border-amber-700 text-amber-300" : character === "moderate" ? "border-sky-800 text-sky-300" : "border-zinc-700 text-zinc-400"}`} title={character === "aggressive" ? "Aggressive playbook: failures are fast, so risk per trade is capped at 0.5% of equity." : character === "moderate" ? "Moderate playbook: standard sizing, a bit more room for the trade to be wrong." : "Standard playbook: sized at your chosen risk profile."}>{character}</span>}
        {busy && <span className="text-sm text-sky-300 animate-pulse">{job!.state === "queued" ? "Queued…" : "Researching…"}</span>}
        {job?.state === "failed" && !researched && <span className="text-sm text-red-400" title={job.error ?? ""}>Research failed</span>}
        {held.length > 0 && (
          <button onClick={() => navigate("prop")} className="text-sm px-2 py-1 rounded border border-sky-800/60 text-sky-300">
            {held.length === 1 ? `OPEN · ${held[0].unrealizedUsd != null ? `${held[0].unrealizedUsd >= 0 ? "+" : ""}$${held[0].unrealizedUsd.toFixed(0)}` : ""} ${held[0].unrealizedR != null ? `· ${held[0].unrealizedR >= 0 ? "+" : ""}${held[0].unrealizedR.toFixed(2)}R` : ""} · ${held[0].status === "hold" ? "thesis intact" : held[0].status.replace("_", " ")}` : `OPEN IN ${held.length} ACCOUNTS`} · View
          </button>
        )}
        {pending.length > 0 && <button onClick={() => navigate("prop")} className="text-sm px-2 py-1 rounded border border-zinc-700 text-zinc-400">{pending.some((p) => p.state === "ready") ? "TRADE READY · View" : "Waiting for entry · View"}</button>}
        <div className="ml-auto flex items-center gap-2">
          {(!s || (s.quality === "weak" && !s.pinned && !researched)) && <button onClick={a.onQueue} className={primary} title="Move this coin into Needs Research. Free; nothing is researched until you click Research.">Add to Research Queue</button>}
          {s && !researched && (s.quality !== "weak" || s.pinned) && <button onClick={a.onResearch} disabled={busy} className={primary} title="One model call with bounded web search. Runs on the server; you can leave this page.">{busy ? "Researching…" : "Research"}</button>}
          {s && researched && rs === "stale" && <button onClick={a.onResearch} disabled={busy} className="px-3 py-1.5 rounded bg-amber-300 text-zinc-900 text-sm font-semibold uppercase tracking-wide disabled:opacity-40" title="Conditions changed since the last research. One model call.">{busy ? "Researching…" : "Refresh Research"}</button>}
          {s && researched && rs !== "stale" && rec === "enter" && <button onClick={a.onBuild} className={primary}>Build Trade</button>}
          {s && researched && rs !== "stale" && rec === "wait" && <button onClick={a.onBuild} className={primary}>View Plan</button>}
          {s && researched && rs !== "stale" && rec === "pass" && <button onClick={a.onDismiss} className={primary}>Dismiss Setup</button>}
          {s && researched && rs === "aging" && <button onClick={a.onResearch} disabled={busy} className={quiet} title="One model call">Refresh</button>}
          <button onClick={a.onTrade} className={quiet} title="Manual ticket: your side, your account, Wick's plan as a starting point">Trade Anyway</button>
          {s && researched && <button onClick={a.onClear} className={quiet} title="Back to NOT RUN. Keeps the setup and all history; spends nothing.">Clear Research</button>}
          {s && !researched && (s.quality !== "weak" || s.pinned) && <button onClick={a.onDismiss} className={quiet} title="Back to Watching">Dismiss</button>}
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[13px]">
        {chips.map((c) => (
          <Term key={c.name} k={(KEY[c.name] ?? "trend") as never} value={c.ok} detail={c.detail} ctx={{ symbol: m.symbol, atrPct: f.atrDailyPct }} className={c.ok ? "text-emerald-400/90" : c.ok === false ? "text-red-400/90" : "text-zinc-500"}>
            <span className="cursor-help">{c.name} {c.ok ? "✓" : c.ok === false ? "✗" : "·"}</span>
          </Term>
        ))}
      </div>
      {s && s.concern !== "none" && (
        <div className="text-sm text-zinc-300"><Term k="mainConcern" className="text-zinc-500"><span className="cursor-help">Main concern:</span></Term> {explainConcern(s.concern, concernCheck?.detail)}</div>
      )}
      {s && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 text-sm">
          <div className="rounded bg-zinc-900/60 p-2">
            <Term k="quantSignal" value={s.liveSignal}><div className="text-xs uppercase tracking-wide text-zinc-500 cursor-help">Quant Signal</div></Term>
            <div className="mt-1 flex items-center gap-2"><Stance s={s.liveSignal} /><span className="text-zinc-300 text-sm">{s.liveSignal === "flat" ? "No playbook matches right now." : `${m.rules?.playbook ?? "Playbook"} favors a ${s.liveSignal}${m.rules?.entryAction === "wait" ? "; extended, wait for a pullback" : ""}.`}</span></div>
            <div className="text-xs text-zinc-500 mt-0.5">Updates every scan</div>
          </div>
          <div className={`rounded p-2 ${rs === "stale" ? "bg-amber-950/20 border border-amber-800/50" : "bg-zinc-900/60"}`}>
            <Term k="aiResearch"><div className="text-xs uppercase tracking-wide text-zinc-500 cursor-help">AI Research</div></Term>
            {!researched ? (
              <div className="mt-1 text-zinc-300 font-semibold">NOT RESEARCHED</div>
            ) : (
              <div className="mt-1">
                <div className="text-zinc-300 font-semibold uppercase text-xs tracking-wide">Researched {ago(s.researchAgeS)}</div>
                <div className={`font-semibold ${rs === "stale" ? "text-amber-300" : rs === "aging" ? "text-zinc-200" : "text-emerald-300"}`}>
                  {rs === "stale" ? `STALE — ${(stale1 ?? "").toUpperCase()}` : rs === "aging" ? "AGING" : "CURRENT"}
                </div>
                {r && <div className={r.context === "adverse" ? "text-red-300" : r.context === "supportive" ? "text-emerald-300" : "text-zinc-200"}>
                  <span className="font-semibold uppercase">{r.context ?? r.thesisVerdict}</span>
                  {" — "}{stripMd(r.keyReason ?? r.summary)}
                  <span className="text-zinc-500"> {r.confidence} confidence.</span>
                </div>}
              </div>
            )}
          </div>
          <div className="rounded bg-zinc-900/60 p-2">
            <Term k="wickVerdict"><div className="text-xs uppercase tracking-wide text-zinc-500 cursor-help">Wick Verdict</div></Term>
            <div className="mt-1 text-zinc-100">
              <span className="font-semibold uppercase">{!researched ? "Research first" : rs === "stale" ? "Refresh research" : rec === "enter" ? `Enter${character === "aggressive" ? " · aggressive" : ""}` : rec === "wait" ? "Wait" : "Pass"}</span>
              {" — "}
              <span className="text-zinc-300">{!researched ? "the numbers look interesting, but Wick has not checked the news yet." : rs === "stale" ? "conditions changed since Wick last looked; refresh before acting." : rec === "enter" ? (r?.context === "supportive" ? "build the trade; the playbook and the news agree." : `build the trade on the ${playbook ?? "quant"} setup; nothing in the news argues against it.`) : rec === "wait" ? (s.liveSignal === "flat" ? "the news is interesting but no playbook matches yet; keep watching." : "the idea holds, not at this price. " + stripMd(r?.mainRisk ?? "")) : "do not trade this. " + stripMd(r?.mainRisk ?? "")}</span>
            </div>
          </div>
        </div>
      )}
      {r && researched && (
        <div className="rounded bg-zinc-900/40 p-2 text-sm">
          <div className="text-zinc-300">{stripMd(r.summary)}</div>
          {r.drivers.length > 0 && <div className="text-zinc-500 mt-0.5">{r.drivers.slice(0, 4).map((d, i) => <span key={i} className={r.catalysts?.[i]?.impact === "negative" ? "text-red-300/80" : r.catalysts?.[i]?.impact === "positive" ? "text-emerald-300/80" : ""}>{stripMd(d.text)}{d.url && <a href={d.url} target="_blank" rel="noreferrer" className="text-sky-400 ml-1">↗</a>}{i < Math.min(4, r.drivers.length) - 1 ? " · " : ""}</span>)}</div>}
          {r.drivers.length === 0 && <div className="text-zinc-500 mt-0.5">Nothing material found in the last few days. That is neutral, not a reason to skip.</div>}
        </div>
      )}
      <button onClick={() => setOpen((o) => !o)} className="self-start text-[13px] text-zinc-500 hover:text-zinc-300">{open ? "▾ Quant Details" : "▸ Quant Details: shape, judges, flow, slippage, base rates"}</button>
      {open && <MoverDetails m={{ ...m, model: researched ? m.model : null }} budget={budget} account={account} onRun={a.onRun} running={a.running} />}
    </section>
  );
}

/** Is it the market or the coin? Share of tracked coins above their averages, plus implied vol. */
function BreadthCard({ b, dvol, n }: { b: AnalysisPayload["breadth"]; dvol: Record<string, number>; n: number }) {
  const pct = (x?: number) => (x == null ? "–" : `${(x * 100).toFixed(0)}%`);
  return (
    <div className="rounded border border-zinc-800 p-4 text-sm">
      <div className="text-[13px] uppercase tracking-wide text-zinc-500 mb-1">Market Breadth ({n} tracked)</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 num">
        <span className="text-zinc-400">Above 200 MA</span><span className="text-right">{pct(b.above200)}</span>
        <span className="text-zinc-400">Above 50 MA</span><span className="text-right">{pct(b.above50)}</span>
        <span className="text-zinc-400">Above 20 MA</span><span className="text-right">{pct(b.above20)}</span>
        <span className="text-zinc-400">Up on the day</span><span className="text-right">{pct(b.up24h)}</span>
        {Object.entries(dvol).map(([k, v]) => <span key={k} className="contents"><span className="text-zinc-400" title="Deribit DVOL: 30-day implied volatility, annualized">{k} implied vol</span><span className="text-right">{v.toFixed(1)}%</span></span>)}
      </div>
    </div>
  );
}

function Board({ name, s }: { name: string; s: PaperPayload["scoreboard"]["rules"] }) {
  return (
    <div className="rounded border border-zinc-800 p-4 text-sm">
      <div className="text-[13px] uppercase tracking-wide text-zinc-500 mb-1">{name} Judge · Scoreboard</div>
      {s.n === 0 ? <div className="text-zinc-500">No closed calls yet.</div> : (
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 num">
          <span className="text-zinc-400">Closed calls</span><span className="text-right">{s.n}</span>
          <span className="text-zinc-400">Hit rate (net)</span><span className="text-right">{((s.hitRate ?? 0) * 100).toFixed(0)}%</span>
          <span className="text-zinc-400">Equity</span><span className="text-right">${s.equity.toLocaleString()} ({fmtPct(s.returnPct)})</span>
          <span className="text-zinc-400">Max drawdown</span><span className="text-right">{fmtNum(s.maxDrawdownPct, 2)}%</span>
          <span className="text-zinc-400">Worst day</span><span className="text-right">{fmtNum(s.worstDayPct, 2)}%</span>
        </div>
      )}
    </div>
  );
}

const HOW_WICK_UPDATES = "Quant scan: every 15 minutes and on Scan Now, no AI cost; it checks every tracked coin against six playbooks. A coin enters Needs Research when it matches a playbook or ranks in the top movers, and leaves when that stops. Trade monitoring: every minute. AI research: only when you press Research; it runs on the server and finishes even if you leave the page.";

export default function Analysis({ params }: { params: URLSearchParams }) {
  const focus = params.get("symbol");
  const [data, setData] = useState<AnalysisPayload | null>(() => peek<AnalysisPayload>("/api/analysis") ?? null);
  const [paper, setPaper] = useState<PaperPayload | null>(() => peek<PaperPayload>("/api/paper") ?? null);
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [researching, setResearching] = useState<number | null>(null);
  const [ticket, setTicket] = useState<{ setupId?: number; symbol: string } | null>(null);
  const [usageOpen, setUsageOpen] = useState(false);
  const [menu, setMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useClickOutside(menuRef, () => setMenu(false), menu);
  const [tab, setTab] = useLocalStorage<"needs" | "researched" | "watching">("analysisTab", "needs");
  const [moreOpen, setMoreOpen] = useState(false);

  useEffect(() => {
    if (!focus) return;
    const el = document.getElementById(`mover-${focus}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focus, data?.lastScan]);

  const load = () => {
    api.analysis().then(setData).catch((e) => setError(String(e.message)));
    api.paper().then(setPaper).catch(() => {});
  };
  useEffect(() => { load(); const t = setInterval(load, 30_000); return () => clearInterval(t); }, []);

  const guard = async (fn: () => Promise<unknown>) => { setError(null); try { await fn(); load(); } catch (e) { setError(String((e as Error).message)); } };
  const research = async (id: number) => { setResearching(id); await guard(() => api.researchSetup(id)); setResearching(null); };
  const run = async (symbol: string) => { setRunning(symbol); await guard(() => api.runAnalysis(symbol)); setRunning(null); };
  const [scanning, setScanning] = useState(false);
  const scanNow = async () => { setScanning(true); await guard(() => api.scan()); setScanning(false); };
  // Research jobs and scans finish on the server; the page just reloads its payload when told.
  useServerMessages((m) => { if (m.type === "research" || m.type === "scan") load(); }, []);

  if (!data) return <div className="p-3 text-[15px] text-zinc-500">{error ?? "Loading…"}</div>;
  const llm = data.llm;
  // Focused coin first, then newest setups first so what you just added is at the top.
  const ordered = [...data.movers].sort((x, y) => (x.symbol === focus ? -1 : y.symbol === focus ? 1 : (y.setup?.detectedAt ?? 0) - (x.setup?.detectedAt ?? 0)));
  const needs = ordered.filter((m) => m.setup && m.setup.state !== "researched" && (m.setup.quality !== "weak" || m.setup.pinned));
  const researched = ordered.filter((m) => m.setup && m.setup.state === "researched" && m.setup.researchState !== "none");
  const watching = ordered.filter((m) => !needs.includes(m) && !researched.includes(m));
  const stale = researched.filter((m) => m.setup!.researchState === "stale").length;
  const attention = data.desk?.attention ?? 0;
  const actions = (m: Mover): CardActions => ({
    onResearch: () => m.setup && research(m.setup.id),
    onDismiss: () => m.setup && guard(() => api.passSetup(m.setup!.id)),
    onBuild: () => m.setup && setTicket({ setupId: m.setup.id, symbol: m.symbol }),
    onTrade: () => setTicket({ symbol: m.symbol }),
    onClear: () => m.setup && guard(() => api.clearResearch(m.setup!.id)),
    onQueue: () => guard(() => api.pinSetup(m.symbol)),
    onRun: () => run(m.symbol), running: running === m.symbol, researching: m.setup?.id === researching,
  });
  const card = (m: Mover) => <MoverCard key={m.symbol} m={m} budget={data.dailyLossBudgetPct} account={data.accountSize} a={actions(m)} focused={m.symbol === focus} />;
  const warming = (data.warming ?? []).map((sym) => (
    <section key={`warm-${sym}`} className="rounded border border-sky-800/60 bg-sky-950/10 p-4 flex items-center gap-3">
      <span className="text-[17px] font-semibold">{sym}</span>
      <span className="text-sm text-sky-300 animate-pulse">Warming market history…</span>
      <span className="text-sm text-zinc-500">Downloading candles and running the quant scan. Usually a few seconds.</span>
    </section>
  ));

  return (
    <div className="p-3 flex flex-col gap-3 max-w-6xl">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-[15px]">
        <span className="text-[13px] uppercase tracking-wide text-zinc-500">Analysis</span>
        <span className="num"><span className="font-semibold text-zinc-100">{needs.length}</span> need research · <span className="font-semibold text-emerald-300">{researched.length - stale}</span> researched{stale > 0 && <> · <span className="font-semibold text-amber-300">{stale}</span> stale</>}</span>
        {attention > 0 && <button onClick={() => navigate("prop")} className="text-amber-300 hover:underline">{attention} open {attention === 1 ? "position needs" : "positions need"} attention →</button>}
        <span className="text-sm text-zinc-500 num" title={HOW_WICK_UPDATES}>
          Last scan {data.lastScan ? fmtClock(data.lastScan) : "pending"} · next {data.nextScan ? fmtClock(data.nextScan) : "–"} · {data.scanStats?.checked ?? 0} markets checked · {data.scanStats?.interesting ?? 0} match a playbook · free <span className="text-zinc-400">ⓘ</span>
        </span>
        <button onClick={scanNow} disabled={scanning} className="text-sm px-2 py-0.5 rounded border border-zinc-800 text-zinc-400 hover:text-zinc-200 disabled:opacity-50" title="Re-run the quant scan on every tracked coin now. Free.">{scanning ? "Scanning…" : "Scan Now"}</button>
        <button onClick={() => setUsageOpen((o) => !o)} className={`text-sm num hover:underline ${llm.error ? "text-amber-400" : "text-zinc-500"}`} title={llm.error ?? `${llm.configured ? llm.model : "model off"}`}>
          AI Research · {(data.research?.running ?? 0) > 0 || (data.research?.queued ?? 0) > 0 ? `${data.research.running} running · ${data.research.queued} queued · ` : ""}{llm.usage.calls} calls today · ~${llm.usage.estCostUsd.toFixed(2)}
        </button>
        {error && <span className="text-red-400 text-sm">{error}</span>}
        <div className="ml-auto relative" ref={menuRef}>
          <button onClick={() => setMenu((o) => !o)} className="px-2 py-1 rounded border border-zinc-800 text-zinc-400 hover:text-zinc-200 text-sm tracking-widest">•••</button>
          {menu && (
            <div className="absolute right-0 z-20 mt-1 w-60 rounded border border-zinc-700 bg-zinc-900 shadow-xl text-sm py-1">
              <button onClick={() => { setMenu(false); guard(() => api.clearResearch()); }} className="block w-full text-left px-3 py-1.5 hover:bg-zinc-800 text-zinc-200" title="Every setup back to NOT RUN. Keeps history, trades and the usage log.">Clear All Research</button>
              <button onClick={() => { setMenu(false); if (window.confirm("Reset the analysis workspace? Setups start from square one at the next scan. Candles, accounts, trades and the usage log are kept.")) guard(() => api.resetWorkspace()); }} className="block w-full text-left px-3 py-1.5 hover:bg-zinc-800 text-zinc-400">Reset Analysis Workspace</button>
            </div>
          )}
        </div>
      </div>
      {usageOpen && (
        <div className="rounded border border-zinc-800 p-3 text-sm">
          <div className="text-xs uppercase tracking-wide text-zinc-500 mb-1">AI Research Calls Today · Estimated at $0.20 per million input tokens, $1.20 per million output tokens, plus one web search each</div>
          {llm.usage.recent.length === 0 ? <div className="text-zinc-500">None yet. The model only runs when you click Research or Refresh Research.</div> : (
            <table className="w-full num"><tbody>
              {llm.usage.recent.map((c, i) => (
                <tr key={i} className="border-t border-zinc-900 text-zinc-400"><td className="py-1 text-zinc-200">{c.symbol.split("#")[0]}</td><td>{fmtTime(c.time).slice(5, 16)}</td><td>{c.trigger === "auto" ? "Automatic" : c.trigger === "position" ? "Position review" : "You clicked"}</td><td className="text-right">{c.inputTokens.toLocaleString()} in · {c.outputTokens.toLocaleString()} out</td><td className="text-right">~${c.estCostUsd.toFixed(3)}</td></tr>
              ))}
            </tbody></table>
          )}
        </div>
      )}

      {focus && !data.movers.some((m) => m.symbol === focus) && (
        <div className="rounded border border-sky-800/60 bg-sky-950/10 p-4 flex flex-wrap items-center gap-3">
          <span className="font-semibold text-[17px]">{focus}</span>
          <span className="text-zinc-400">Now tracked. Its history is downloading and it joins the scan within about two minutes.</span>
          <button onClick={() => setTicket({ symbol: focus })} className="ml-auto px-3 py-1.5 rounded bg-zinc-100 text-zinc-900 text-sm font-semibold uppercase">Trade Anyway</button>
        </div>
      )}

      <div className="flex gap-1 border-b border-zinc-800 mt-1">
        {([["needs", "Needs Research", needs.length], ["researched", "Researched", researched.length], ["watching", "Watching", watching.length]] as const).map(([k, label, n]) => (
          <button key={k} onClick={() => setTab(k)} className={`px-4 py-2 text-[15px] border-b-2 -mb-px ${tab === k ? "border-zinc-100 text-zinc-100 font-semibold" : "border-transparent text-zinc-400 hover:text-zinc-200"}`}>
            {label} <span className="num text-zinc-500">{n}</span>{k === "researched" && stale > 0 && <span className="ml-1 text-amber-300 num">· {stale} stale</span>}
          </button>
        ))}
      </div>
      {warming}
      {tab === "needs" && needs.length === 0 && <div className="text-sm text-zinc-500">Nothing new. Every scan (each 15 minutes, or Scan Now) adds a coin here when it matches a playbook or ranks in the top movers; it leaves when that stops being true, or when you dismiss or research it.</div>}
      {tab === "needs" && needs.map(card)}
      {tab === "researched" && researched.length === 0 && <div className="text-sm text-zinc-500">No research yet. Click Research on a setup in Needs Research; one model call, cached until you clear or refresh it.</div>}
      {tab === "researched" && researched.map(card)}
      {tab === "watching" && <div className="text-sm text-zinc-500">Tracked coins that are not a setup right now. Wick keeps scanning them. Add one to the research queue if you want to work on it.</div>}
      {tab === "watching" && watching.map(card)}
      {data.movers.length === 0 && <div className="text-[15px] text-zinc-500">Waiting for the first scan (about 20s after the backend starts).</div>}

      <button onClick={() => setMoreOpen((o) => !o)} className="self-start text-[13px] uppercase tracking-wide text-zinc-500 hover:text-zinc-300 mt-2">{moreOpen ? "▾" : "▸"} Scoreboards & Market Breadth</button>
      {moreOpen && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <BreadthCard b={data.breadth} dvol={data.dvol} n={data.movers.length} />
            {paper && <Board name="Rules" s={paper.scoreboard.rules} />}
            {paper && <Board name="Model" s={paper.scoreboard.model} />}
          </div>
          {paper && (paper.open.length > 0 || paper.closed.length > 0) && (
            <section className="rounded border border-zinc-800 p-4">
              <div className="text-[13px] uppercase tracking-wide text-zinc-500 mb-1">Recommendation Log</div>
              <div className="max-h-72 overflow-auto">
                <table className="w-full text-sm num">
                  <thead className="text-zinc-500 sticky top-0 bg-zinc-950"><tr>
                    <th className="text-left font-normal">Opened (UTC)</th><th className="text-left font-normal">Symbol</th><th className="text-left font-normal">Judge</th><th className="text-left font-normal">Stance</th>
                    <th className="text-right font-normal">Entry</th><th className="text-right font-normal">Stop</th><th className="text-right font-normal">+24h</th><th className="text-left font-normal">Closed</th><th className="text-right font-normal">Net P&L</th>
                  </tr></thead>
                  <tbody>
                    {[...paper.open, ...paper.closed].map((r) => (
                      <tr key={r.id} className="border-t border-zinc-900 text-zinc-400">
                        <td>{fmtTime(r.time).slice(0, 16)}</td><td className="text-zinc-200">{r.symbol}</td><td className="capitalize">{r.source}</td><td><Stance s={r.stance} small /></td>
                        <td className="text-right">{fmtPrice(r.entry)}</td><td className="text-right">{fmtPrice(r.invalidation)}</td>
                        <td className="text-right">{r.ret_24h == null ? "…" : fmtPct(r.ret_24h * 100)}</td>
                        <td>{r.closed_at ? `${r.exit_reason} ${fmtTime(r.closed_at).slice(5, 16)}` : "open"}</td>
                        <td className={`text-right ${r.pnl_pct == null ? "" : r.pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>{r.pnl_pct == null ? "" : fmtPct(r.pnl_pct * 100)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}
      {ticket && <TradeTicket setupId={ticket.setupId} symbol={ticket.symbol} onClose={() => setTicket(null)} onDone={() => { setTicket(null); load(); navigate("prop"); }} />}
    </div>
  );
}
