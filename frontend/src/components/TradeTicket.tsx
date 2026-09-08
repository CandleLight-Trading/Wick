import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtCompact, fmtPrice, useLocalStorage } from "../store";
import type { Account, PlanResponse } from "../types";

/**
 * The order ticket. Guided (from a Wick setup) or manual (any symbol, your side).
 * Account + risk profile + the calculated plan + one button.
 *
 * Wick's opinion is advice: a PASS or an unresearched setup turns the button into
 * TAKE IT ANYWAY, which works. The account's own rules (open-risk cap, daily loss budget,
 * breached status) are the only thing that disables it. Manual edits live under Advanced.
 */
export default function TradeTicket({ setupId, symbol, side: fixedSide, onClose, onDone }: {
  setupId?: number; symbol: string; side?: "long" | "short"; onClose: () => void; onDone: () => void;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useLocalStorage<number | null>("propAccount", null);
  const [side, setSide] = useState<"long" | "short">(fixedSide ?? "long");
  const [risk, setRisk] = useState("standard");
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [ov, setOv] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [chooseSize, setChooseSize] = useState(false);
  const [capped, setCapped] = useState<string | null>(null);

  useEffect(() => {
    api.accounts().then((a) => {
      setAccounts(a);
      if (accountId == null || !a.some((x) => x.id === accountId)) setAccountId(a[0]?.id ?? null);
    }).catch((e) => setError(String(e.message)));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch the plan; a freshly tracked coin has no history for a few seconds, so retry quietly.
  useEffect(() => {
    if (accountId == null) return;
    let cancelled = false, tries = 0;
    setPlan(null); setError(null);
    const attempt = () => {
      api.plan({ setupId, symbol, side: setupId ? undefined : side, accountId, risk }).then((p) => {
        if (cancelled) return;
        setPlan(p); setLoading(null);
        if (!setupId && p.plan.side !== side) setSide(p.plan.side);
      }).catch((e) => {
        if (cancelled) return;
        const msg = String(e.message);
        if (/loading|no price|history/i.test(msg) && tries++ < 20) { setLoading("Fetching market data for this coin…"); setTimeout(attempt, 3000); }
        else { setLoading(null); setError(msg); }
      });
    };
    attempt();
    return () => { cancelled = true; };
  }, [setupId, symbol, side, accountId, risk]);

  const submit = async (force: boolean) => {
    if (!plan || accountId == null) return;
    setBusy(true); setError(null);
    const overrides: Record<string, number | boolean | null> = {};
    for (const [k, v] of Object.entries(ov)) {
      if (k === "marketNow") { if (v === "1") overrides.marketNow = true; continue; }
      if (k === "target" && v === "none") { overrides.target = null; continue; }
      if (v !== "") overrides[k] = Number(v);
    }
    try {
      await api.createTrade({ account_id: accountId, setup_id: setupId, symbol: setupId ? undefined : symbol, side: setupId ? undefined : side, risk_profile: risk, overrides, force });
      onDone();
    } catch (e) { setError(String((e as Error).message)); }
    setBusy(false);
  };

  const p = plan?.plan;
  const s = plan?.sizing;
  const acct = plan?.account;
  const dir = p?.side === "long" ? 1 : -1;
  const entry = Number(ov.entry || p?.entry || 0);
  const stop = Number(ov.stop || p?.invalidation || 0);
  const target = ov.target === "none" ? null : ov.target ? Number(ov.target) : p?.target ?? null;
  const size = Number(ov.sizeUsd || s?.notionalUsd || 0);
  const riskUsd = entry && stop ? (size * dir * (entry - stop)) / entry : 0;
  const rr = target != null && entry && stop ? (dir * (target - entry)) / (dir * (entry - stop)) : null;
  const maxSize = s?.maxNotionalUsd ?? 0;
  const recommended = s?.notionalUsd ?? 0;
  const aboveRec = size > recommended * 1.001;
  // Sizing beyond the recommendation is allowed; beyond the account ceiling is not.
  const hardBlocked = !!s?.blocked || !!acct?.breached || (maxSize > 0 && size > maxSize * 1.0001);
  const advisory = !!p && (p.recommendation === "pass" || (!!setupId && !plan?.researched));
  const waiting = p?.action === "wait" && ov.marketNow !== "1";
  const setSize = (raw: string, cap = true) => {
    const n = Number(raw.replace(/[^0-9.]/g, ""));
    if (!raw || Number.isNaN(n)) { setOv({ ...ov, sizeUsd: "" }); setCapped(null); return; }
    if (cap && maxSize > 0 && n > maxSize) { setOv({ ...ov, sizeUsd: String(Math.floor(maxSize)) }); setCapped(`Capped at $${Math.floor(maxSize).toLocaleString()}: larger would exceed the account's open-risk or daily-loss limit.`); return; }
    setCapped(null); setOv({ ...ov, sizeUsd: String(Math.round(n)) });
  };
  const Row = ({ k, v, tone }: { k: string; v: React.ReactNode; tone?: string }) => (
    <><span className="text-zinc-400">{k}</span><span className={`text-right num ${tone ?? "text-zinc-100"}`}>{v}</span></>
  );

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div className="w-full max-w-lg max-h-[92vh] overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-950 p-5 text-[15px] shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-baseline justify-between mb-4">
          <span className="text-[13px] uppercase tracking-wide text-zinc-500">Trade ticket{plan?.manual ? " · manual" : ""}</span>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-200 text-xl leading-none">✕</button>
        </div>

        <div className="flex items-baseline gap-3 mb-4">
          {!setupId ? (
            <div className="inline-flex rounded border border-zinc-700 overflow-hidden text-[15px] font-semibold">
              {(["long", "short"] as const).map((x) => (
                <button key={x} onClick={() => setSide(x)} className={`px-3 py-1 uppercase ${side === x ? (x === "long" ? "bg-emerald-600 text-white" : "bg-red-600 text-white") : "text-zinc-400 hover:bg-zinc-800"}`}>{x}</button>
              ))}
            </div>
          ) : (
            <span className={`text-xl font-semibold uppercase ${p?.side === "long" ? "text-emerald-400" : "text-red-400"}`}>{p?.side ?? side}</span>
          )}
          <span className="text-2xl font-semibold">{symbol}</span>
        </div>

        <div className="grid grid-cols-2 gap-3 mb-4 text-sm">
          <label className="flex flex-col gap-1"><span className="text-zinc-500">Account</span>
            <select value={accountId ?? ""} onChange={(e) => setAccountId(Number(e.target.value))} className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1.5 text-[15px]">
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.name} · ${fmtCompact(a.equity)}</option>)}
            </select>
          </label>
          <div className="flex flex-col gap-1"><span className="text-zinc-500">Risk profile</span>
            <div className="inline-flex rounded border border-zinc-800 overflow-hidden">
              {Object.entries(plan?.riskProfiles ?? { conservative: 0.5, standard: 0.75, aggressive: 1.0 }).map(([k, v]) => (
                <button key={k} onClick={() => setRisk(k)} className={`flex-1 px-2 py-1.5 capitalize ${risk === k ? "bg-zinc-700 text-white" : "text-zinc-400 hover:bg-zinc-800"}`} title={`${v}% of equity at risk`}>{k}</button>
              ))}
            </div>
          </div>
        </div>

        {loading && <div className="text-zinc-400 mb-3">{loading}</div>}
        {!plan && !error && !loading && <div className="text-zinc-500">Building plan…</div>}
        {p && s && acct && (
          <>
            <div className={`rounded border p-3 mb-4 ${advisory ? "border-amber-800/60 bg-amber-950/10" : "border-zinc-800 bg-zinc-900/40"}`}>
              <div className="flex items-baseline gap-2">
                <span className="text-[13px] uppercase tracking-wide text-zinc-500">Wick says</span>
                <span className={`text-xl font-semibold uppercase ${advisory ? "text-amber-300" : "text-zinc-100"}`}>{p.recommendation}</span>
                {p.trigger_kind && <span className="text-sm text-zinc-500">· {p.trigger_kind} entry, watched every minute</span>}
              </div>
              <div className="text-sm text-zinc-400 mt-1">{setupId && !plan.researched ? "Not researched yet. " : ""}{p.reason}</div>
            </div>

            <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 mb-3">
              <Row k={waiting ? "Entry trigger" : "Entry"} v={fmtPrice(entry)} />
              <Row k="Stop" v={<>{fmtPrice(stop)} <span className="text-zinc-500">({(Math.abs(entry / stop - 1) * 100).toFixed(2)}% away)</span></>} />
              <Row k="Target" v={target != null ? fmtPrice(target) : <span className="text-zinc-500">none · manage it yourself</span>} />
              <Row k="R:R" v={rr != null ? rr.toFixed(2) : "–"} tone={rr == null ? "text-zinc-500" : rr >= 1.5 ? "text-emerald-400" : "text-amber-300"} />
            </div>

            {/* Position size: Wick's recommendation is the default; choosing your own is a deliberate step. */}
            <div className="rounded border border-zinc-800 bg-zinc-900/40 p-3 mb-3">
              <div className="flex items-baseline justify-between">
                <span className="text-[13px] uppercase tracking-wide text-zinc-500">Position size · exposure</span>
                <button onClick={() => { setChooseSize((c) => !c); if (chooseSize) { setOv({ ...ov, sizeUsd: "" }); setCapped(null); } }} className="text-sm text-zinc-400 hover:text-zinc-100">{chooseSize ? "Use Wick's Size" : "Choose My Amount"}</button>
              </div>
              {!chooseSize ? (
                <div className="mt-1 flex items-baseline gap-3">
                  <span className="text-2xl font-semibold num">${Math.round(recommended).toLocaleString()}</span>
                  <span className="text-sm text-zinc-400 num">Wick recommended · loss at stop ${s.riskUsd.toFixed(0)} · {s.riskPct}% of equity</span>
                </div>
              ) : (
                <div className="mt-2">
                  <div className="flex items-center gap-3">
                    <span className="text-zinc-500 text-xl">$</span>
                    <input value={ov.sizeUsd ?? String(Math.round(recommended))} onChange={(e) => setSize(e.target.value, false)} onBlur={(e) => setSize(e.target.value)} className="bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-2xl font-semibold num w-44" />
                    <span className="text-sm text-zinc-500 num">max ${Math.floor(maxSize).toLocaleString()}</span>
                  </div>
                  <div className="relative mt-3 mb-1">
                    <input type="range" min={0} max={Math.max(1, Math.floor(maxSize))} step={Math.max(1, Math.round(maxSize / 200))} value={Math.min(size, maxSize)} onChange={(e) => setSize(e.target.value)} className="w-full" />
                    {maxSize > 0 && <div className="absolute -top-1 h-5 w-0.5 bg-sky-400 pointer-events-none" style={{ left: `${Math.min(100, (recommended / maxSize) * 100)}%` }} title="Wick's recommendation" />}
                  </div>
                  <div className="flex justify-between text-xs text-zinc-500 num"><span>$0</span><span className="text-sky-400">Wick ${Math.round(recommended).toLocaleString()}</span><span>max ${Math.floor(maxSize).toLocaleString()}</span></div>
                  {capped && <div className="text-sm text-amber-300 mt-2">{capped}</div>}
                  {!capped && aboveRec && size <= maxSize && <div className="text-sm text-amber-300/90 mt-2">⚠ Above Wick's {risk} recommendation. Allowed.</div>}
                  {size > maxSize * 1.0001 && <div className="text-sm text-red-400 mt-2">⛔ Account risk limit exceeded. Maximum ${Math.floor(maxSize).toLocaleString()}.</div>}
                </div>
              )}
            </div>

            <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 mb-3">
              <Row k="Loss if the stop fills" v={<>${riskUsd.toFixed(0)} <span className="text-zinc-500">· {((riskUsd / acct.equity) * 100).toFixed(2)}% of equity</span></>} tone={aboveRec ? "text-amber-300" : "text-zinc-100"} />
              <Row k="Portfolio risk after" v={`${((acct.openRiskUsd + riskUsd) / acct.equity * 100).toFixed(2)} / ${s.openRiskCapPct.toFixed(1)}%`} tone={(acct.openRiskUsd + riskUsd) / acct.equity * 100 > s.openRiskCapPct ? "text-red-400" : "text-emerald-400"} />
              <Row k="Daily budget left" v={`$${acct.dailyLossRemainingUsd.toFixed(0)}`} />
              <Row k="Account exposure" v={`${((size / acct.equity) * 100).toFixed(1)}% of equity`} />
            </div>
            {hardBlocked && <div className="rounded border border-red-800 bg-red-950/30 p-2 text-sm text-red-300 mb-3">ACCOUNT RISK LIMIT: {acct.breached ? "account rules already breached" : s.blocks.join("; ")}</div>}

            <button onClick={() => setAdvanced((a) => !a)} className="text-[13px] text-zinc-500 hover:text-zinc-300 mb-2">{advanced ? "▾" : "▸"} Advanced</button>
            {advanced && (
              <div className="grid grid-cols-2 gap-2 text-sm mb-3">
                {(["entry", "stop", "target"] as const).map((k) => (
                  <label key={k} className="flex flex-col gap-0.5"><span className="text-zinc-500">{k}{k === "target" ? " (type none to remove)" : ""}</span>
                    <input value={ov[k] ?? ""} placeholder={String(k === "entry" ? p.entry : k === "stop" ? p.invalidation : p.target ?? "none")} onChange={(e) => setOv({ ...ov, [k]: e.target.value })} className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 num" />
                  </label>
                ))}
                {p.action === "wait" && <label className="col-span-2 flex items-center gap-2 text-zinc-400"><input type="checkbox" checked={ov.marketNow === "1"} onChange={(e) => setOv({ ...ov, marketNow: e.target.checked ? "1" : "" })} /> open at market now instead of waiting for the trigger</label>}
              </div>
            )}

            {error && <div className="text-sm text-red-400 mb-2">{error}</div>}
            <div className="flex gap-2 mt-1 sticky bottom-0 bg-zinc-950 pt-3 pb-1 -mb-1 border-t border-zinc-800">
              {advisory ? (
                <button disabled={busy || hardBlocked} onClick={() => submit(true)} className="flex-1 py-2.5 rounded border-2 border-amber-600 text-amber-200 font-semibold uppercase tracking-wide disabled:opacity-40" title="Wick advises against it. Your call.">
                  {busy ? "Working…" : "Take it anyway"}
                </button>
              ) : (
                <button disabled={busy || hardBlocked} onClick={() => submit(false)} className="flex-1 py-2.5 rounded bg-zinc-100 text-zinc-900 font-semibold uppercase tracking-wide disabled:opacity-40">
                  {busy ? "Working…" : waiting ? `Place waiting order · $${Math.round(size).toLocaleString()}` : `Open $${Math.round(size).toLocaleString()} position`}
                </button>
              )}
              <button onClick={onClose} className="px-4 py-2 rounded border border-zinc-700 text-zinc-400">Cancel</button>
            </div>
          </>
        )}
        {error && !plan && !loading && <div className="text-sm text-red-400">{error}</div>}
      </div>
    </div>
  );
}
