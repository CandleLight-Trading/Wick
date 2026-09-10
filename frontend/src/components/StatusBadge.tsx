import { useConnectionStatus } from "../ws";

const COLORS = { live: "bg-emerald-500", reconnecting: "bg-amber-500", stale: "bg-red-500" };

/** live / reconnecting / stale, plus the REST weight budget and dropped-frame count. */
export default function StatusBadge() {
  const { state, status } = useConnectionStatus();
  const s = status;
  return (
    <div className="flex items-center gap-3 text-sm text-zinc-400 num">
      {s?.rest.banned && (
        <span className="px-2 py-1 rounded bg-red-700 text-white font-semibold">{s.rest.banMessage}</span>
      )}
      {s && (
        <span title="Operational: how much of Binance's per-minute request allowance Wick has used (cap 1200, Wick throttles at 900). Only matters if it climbs near the cap.">
          weight {s.rest.usedWeight1m}
        </span>
      )}
      {s && s.broadcast.droppedFrames > 0 && (
        <span title="Frames dropped because this browser could not keep up">dropped {s.broadcast.droppedFrames}</span>
      )}
      {s && s.ingest.reconnects > 0 && <span title="Operational: how many times the backend has reconnected its Binance data feed since start. Binance closes every connection after 24h, so a small number is routine; gaps are repaired automatically.">reconnects {s.ingest.reconnects}</span>}
      {s && Object.entries(s.exchanges ?? {}).map(([name, ex]) => (
        <span key={name} className="flex items-center gap-1" title={`Second exchange feed (${name}): ${ex.state}, ${ex.tracked.length} of your tracked coins are listed there. Used for the cross-exchange divergence column.`}>
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${ex.state === "live" ? "bg-emerald-500" : "bg-amber-500"}`} />
          {name}
        </span>
      ))}
      {s?.futures && (
        <span title={s.futures.error ?? `Where funding rates and open interest come from (${s.futures.source}). Binance futures is geo-blocked from this host, so Kraken's perpetuals stand in.`} className={s.futures.error ? "text-amber-500" : ""}>
          perps: {s.futures.source ?? "none"}
        </span>
      )}
      <span className="flex items-center gap-1.5">
        <span className={`inline-block w-2 h-2 rounded-full ${COLORS[state]}`} />
        <span className="uppercase tracking-wide">{state}</span>
        {s?.ingest.lastFrameAgeS != null && state !== "reconnecting" && (
          <span className="text-zinc-500">{s.ingest.lastFrameAgeS.toFixed(0)}s</span>
        )}
      </span>
    </div>
  );
}
