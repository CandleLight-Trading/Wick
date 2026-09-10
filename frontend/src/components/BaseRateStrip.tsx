import Term from "./Term";
import { fmtPct } from "../store";
import type { BaseRate, HorizonStats } from "../types";

const HORIZONS = ["1h", "4h", "24h"];

function Cell({ h, minSample }: { h: HorizonStats; minSample: number }) {
  if (h.n === 0) return <td className="px-2 py-1 text-zinc-500">n=0</td>;
  const grey = h.underpowered;
  const pct = (x?: number) => (x == null ? "–" : `${(x * 100).toFixed(0)}%`);
  return (
    <td className={`px-2 py-1 align-top num ${grey ? "text-zinc-500" : ""}`} title={grey ? `n < ${minSample}: not enough episodes to read` : undefined}>
      <div className="flex items-baseline gap-1">
        <span className={`font-semibold ${grey ? "" : "text-zinc-100"}`}>{pct(h.hitRateNet)}</span>
        <span className="text-xs">[{pct(h.ciLo)}–{pct(h.ciHi)}]</span>
        <Term k="brN" value={h.n} className="text-xs text-zinc-500"><span className="cursor-help">n={h.n}</span></Term>
      </div>
      <div className="text-[13px]">
        <Term k="brMedian" value={h.medianNet! * 100}><span className="cursor-help">med {fmtPct(h.medianNet! * 100)}</span></Term> <Term k="brIqr" className="text-zinc-500"><span className="cursor-help">IQR {fmtPct(h.p25Net! * 100)}…{fmtPct(h.p75Net! * 100)}</span></Term>
      </div>
      {h.firstHalf && h.secondHalf && (
        <div className="text-xs text-zinc-500">
          <Term k="brWalk" value={[h.firstHalf.hitRateNet, h.secondHalf.hitRateNet]}><span className="cursor-help">yr1 {pct(h.firstHalf.hitRateNet ?? undefined)} (n={h.firstHalf.n}) → yr2 {pct(h.secondHalf.hitRateNet ?? undefined)} (n={h.secondHalf.n})</span></Term>
        </div>
      )}
      <div className="text-xs text-zinc-500">
        <Term k="brGross"><span className="cursor-help">gross {pct(h.hitRateGross)} / {fmtPct(h.medianGross! * 100)}</span></Term>
        {h.overlapping && <Term k="brOverlap" detail={`median gap ${h.medianGapBars} bars vs a ${h.horizonBars}-bar horizon`} className="ml-1 text-amber-500"><span className="cursor-help">overlapping</span></Term>}
      </div>
    </td>
  );
}

/** Historical base rates for every condition that is true right now. Net-of-cost is the
 *  headline; gross is the small print. Grey means n is too small to read anything into. */
export default function BaseRateStrip({ rates, minSample, tests, cost }: { rates: BaseRate[]; minSample: number; tests: { total: number; symbols: number; conditions: number; horizons: number }; cost: number }) {
  return (
    <div className="rounded border border-amber-900/50 bg-amber-950/10">
      <div className="px-3 py-2 border-b border-amber-900/40 text-[13px] text-amber-200/80 leading-snug">
        <span className="font-semibold uppercase tracking-wide">Historical base rates on limited local data.</span>{" "}
        Not predictive. In-sample, no slippage, survivorship-affected. Net figures deduct {(cost * 100).toFixed(2)}% round-trip fees.
        Any hit rate below is one of <span className="font-semibold text-amber-100">{tests.total} tests</span> evaluated
        ({tests.symbols} symbols × {tests.conditions} conditions × {tests.horizons} horizons); several will look good by chance.
      </div>
      {rates.length === 0 ? (
        <div className="px-3 py-3 text-sm text-zinc-500">No tracked condition is true on the latest closed 1h bar.</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-zinc-500">
            <tr>
              <th className="text-left px-2 py-1 font-normal"><Term k="brCondition"><span className="cursor-help">condition (true now)</span></Term></th>
              <th className="text-left px-2 py-1 font-normal"><Term k="brEpisodes"><span className="cursor-help">episodes</span></Term></th>
              {HORIZONS.map((h) => <th key={h} className="text-left px-2 py-1 font-normal"><Term k="brHit" value={h}><span className="cursor-help">+{h}: hit rate net [95% CI]</span></Term></th>)}
            </tr>
          </thead>
          <tbody>
            {rates.map((r) => (
              <tr key={r.condition} className="border-t border-zinc-800/60">
                <td className="px-2 py-1 align-top text-zinc-200">{r.condition}</td>
                <td className="px-2 py-1 align-top num text-zinc-400">{r.nEpisodes}<span className="text-zinc-500 text-xs"> ({r.nBars} bars)</span></td>
                {HORIZONS.map((h) => <Cell key={h} h={r.horizons[h]} minSample={minSample} />)}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
