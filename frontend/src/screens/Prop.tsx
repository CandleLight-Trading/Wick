import { useEffect, useMemo, useRef, useState } from "react";
import { api, peek } from "../api";
import TradeTicket from "../components/TradeTicket";
import { fmtCompact, fmtNum, fmtPct, fmtPrice, fmtTime, useClickOutside, useLocalStorage } from "../store";
import type { Account, ClosePreview, MarketRow, PositionResearch, PropPayload, SymbolInfo, Trade } from "../types";
import Term from "../components/Term";
import { navigate } from "../App";

const STATUS: Record<string, { label: string; tone: string }> = {
  hold: { label: "HOLD", tone: "text-zinc-400" },
  target_reached: { label: "TARGET REACHED", tone: "text-emerald-300" },
  stop_near: { label: "STOP NEAR", tone: "text-amber-300" },
  shape_flipped: { label: "STRUCTURE CHANGED", tone: "text-amber-300" },
  horizon_expiring: { label: "HORIZON ENDING", tone: "text-amber-300" },
  waiting: { label: "WAITING", tone: "text-zinc-400" },
  ready: { label: "READY", tone: "text-sky-300" },
};
const RANGES = ["1D", "1W", "1M", "ALL"] as const;

function Bar({ used, label }: { used: number; label: string }) {
  const u = Math.min(100, Math.max(0, used));
  return (
    <div>
      <div className="text-sm text-zinc-500 mb-1">{label}</div>
      <div className="h-2 bg-zinc-800 rounded overflow-hidden"><div className={`h-full ${u > 75 ? "bg-red-500" : u > 40 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${u}%` }} /></div>
    </div>
  );
}

const PALETTE = ["#34d399", "#38bdf8", "#f472b6", "#fbbf24", "#a78bfa", "#fb923c", "#2dd4bf", "#f87171"];
export const MAX_COMPARE = 8;

/** Several accounts on one chart, each as percent return from its own starting balance. */
function CompareChart({ series }: { series: { name: string; pts: { time: number; equity: number }[]; start: number }[] }) {
  const w = 1000, h = 240;
  const lines = series.map((s) => ({ name: s.name, ys: s.pts.map((p) => (p.equity / s.start - 1) * 100) })).filter((s) => s.ys.length > 1);
  if (lines.length === 0) return <div className="h-[240px] flex items-center justify-center text-zinc-500">Pick up to {MAX_COMPARE} accounts. Curves appear once each has a minute of history.</div>;
  const all = lines.flatMap((l) => l.ys).concat([0]);
  const min = Math.min(...all), max = Math.max(...all), span = max - min || 1;
  const y = (v: number) => h - ((v - min) / span) * (h - 20) - 10;
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="w-full h-[240px] block">
        <line x1={0} x2={w} y1={y(0)} y2={y(0)} stroke="#3f3f46" strokeDasharray="4 4" />
        {lines.map((l, i) => <polyline key={l.name} points={l.ys.map((v, j) => `${(j / (l.ys.length - 1)) * w},${y(v)}`).join(" ")} fill="none" stroke={PALETTE[i % PALETTE.length]} strokeWidth="2" vectorEffect="non-scaling-stroke" />)}
      </svg>
      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-1 text-sm num">{lines.map((l, i) => <span key={l.name}><span className="inline-block w-2 h-2 rounded-full mr-1 align-middle" style={{ background: PALETTE[i % PALETTE.length] }} />{l.name} <span className={l.ys[l.ys.length - 1] >= 0 ? "text-emerald-400" : "text-red-400"}>{l.ys[l.ys.length - 1] >= 0 ? "+" : ""}{l.ys[l.ys.length - 1].toFixed(2)}%</span></span>)}</div>
    </div>
  );
}

/** The account graph. Minute snapshots when we have them, closed-trade steps otherwise. */
function EquityChart({ pts, start }: { pts: { time: number; equity: number }[]; start: number }) {
  const w = 1000, h = 240;
  if (pts.length < 2) {
    return <div className="h-[240px] flex items-center justify-center text-zinc-500">The curve starts drawing as soon as the account has a position. One point per minute.</div>;
  }
  const ys = pts.map((p) => p.equity);
  const min = Math.min(...ys, start), max = Math.max(...ys, start), span = max - min || 1;
  const x = (i: number) => (i / (pts.length - 1)) * w;
  const y = (v: number) => h - ((v - min) / span) * (h - 20) - 10;
  const up = ys[ys.length - 1] >= start;
  const stroke = up ? "#34d399" : "#f87171";
  const line = pts.map((p, i) => `${x(i)},${y(p.equity)}`).join(" ");
  const area = `0,${h} ${line} ${w},${h}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="w-full h-[240px] block">
      <defs><linearGradient id="eq" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={stroke} stopOpacity="0.25" /><stop offset="100%" stopColor={stroke} stopOpacity="0" /></linearGradient></defs>
      <line x1={0} x2={w} y1={y(start)} y2={y(start)} stroke="#3f3f46" strokeDasharray="4 4" />
      <polygon points={area} fill="url(#eq)" />
      <polyline points={line} fill="none" stroke={stroke} strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

const money = (v: number) => "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** The balance. When a new account has just been funded it rolls up from zero. */
function CountUp({ value, animate, onDone }: { value: number; animate: boolean; onDone: () => void }) {
  const [shown, setShown] = useState(animate ? 0 : value);
  useEffect(() => {
    if (!animate) { setShown(value); return; }
    const t0 = performance.now(), ms = 1800;
    let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / ms);
      setShown(value * (1 - Math.pow(1 - p, 3)));          // ease-out: fast start, settles gently
      if (p < 1) raf = requestAnimationFrame(tick); else onDone();
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [animate, value]); // eslint-disable-line react-hooks/exhaustive-deps
  return <span className={animate ? "text-emerald-300 transition-colors" : undefined}>{money(shown)}</span>;
}

type AccountForm = { name: string; size: number; daily_loss_pct: number; max_dd_pct: number; target_pct: number };

/** Opening an account is an occasion, so it gets a proper dialog rather than an inline strip. */
function NewAccountModal({ form, setForm, onCreate, onClose }: { form: AccountForm; setForm: (f: AccountForm) => void; onCreate: () => Promise<void>; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const field = (k: keyof AccountForm, label: string, hint: string, type: "text" | "number", prefix?: string, suffix?: string) => (
    <label className="flex flex-col gap-1">
      <span className="text-sm text-zinc-300">{label}</span>
      <span className="flex items-center bg-zinc-950 border border-zinc-700 rounded-md px-3 focus-within:border-zinc-400">
        {prefix && <span className="text-zinc-500 mr-1">{prefix}</span>}
        <input type={type} value={String(form[k])} onChange={(e) => setForm({ ...form, [k]: k === "name" ? e.target.value : Number(e.target.value) })} className="bg-transparent py-2 w-full outline-none num text-[17px]" />
        {suffix && <span className="text-zinc-500 ml-1">{suffix}</span>}
      </span>
      <span className="text-xs text-zinc-500">{hint}</span>
    </label>
  );
  return (
    <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="w-full max-w-lg rounded-2xl border border-zinc-700 bg-gradient-to-b from-zinc-900 to-zinc-950 shadow-2xl p-7 flex flex-col gap-5">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-emerald-400">New Account</div>
          <h2 className="text-3xl font-semibold tracking-tight mt-1">Fund a new desk</h2>
          <p className="text-sm text-zinc-400 mt-1">Set the size and the rules you will trade under. Wick enforces the rules; you make the calls.</p>
        </div>
        {field("name", "Name", "How it shows in the account picker.", "text")}
        {field("size", "Starting balance", "The equity you begin with. Position sizes and limits scale from it.", "number", "$")}
        <div className="grid grid-cols-3 gap-3">
          {field("daily_loss_pct", "Daily loss limit", "Lose this much in a day and trading stops until tomorrow.", "number", undefined, "%")}
          {field("max_dd_pct", "Max drawdown", "Fall this far below the peak and the account fails.", "number", undefined, "%")}
          {field("target_pct", "Profit target", "Reach this gain and the challenge is passed.", "number", undefined, "%")}
        </div>
        <div className="flex items-center justify-end gap-3 pt-1">
          <button onClick={onClose} className="px-4 py-2 rounded-md border border-zinc-700 text-zinc-400 hover:text-zinc-200">Cancel</button>
          <button disabled={busy || !form.name.trim() || form.size <= 0} onClick={async () => { setBusy(true); await onCreate(); setBusy(false); }} className="px-6 py-2.5 rounded-md bg-emerald-400 text-zinc-950 font-semibold text-[16px] hover:bg-emerald-300 disabled:opacity-40">
            {busy ? "Funding…" : `Fund ${money(form.size || 0)}`}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Prop: the account. What you bet on, how it is going, how good you are at this. */
export default function Prop() {
  const [accounts, setAccounts] = useState<Account[]>(() => peek<Account[]>("/api/accounts") ?? []);
  const [accountId, setAccountId] = useLocalStorage<number | null>("propAccount", null);
  const [range, setRange] = useLocalStorage<(typeof RANGES)[number]>("propRange", "1W");
  const [data, setData] = useState<PropPayload | null>(() => (accountId != null ? peek<PropPayload>(`/api/prop?account_id=${accountId}&range=${range.toLowerCase()}`) : undefined) ?? null);
  const [celebrate, setCelebrate] = useState<number | null>(null);      // account id whose balance counts up from zero
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "$10K Conservative", size: 10000, daily_loss_pct: 4, max_dd_pct: 8, target_pct: 8 });
  const [review, setReview] = useState<Trade | null>(null);
  const [ticket, setTicket] = useState<string | null>(null);
  const [menu, setMenu] = useState(false);
  const [closing, setClosing] = useState<Trade | null>(null);
  const [researchingId, setResearchingId] = useState<number | null>(null);
  const [compare, setCompare] = useLocalStorage<boolean>("propCompare", false);
  const [compareIds, setCompareIds] = useLocalStorage<number[]>("propCompareIds", []);
  const [compareSeries, setCompareSeries] = useState<{ name: string; pts: { time: number; equity: number }[]; start: number }[]>([]);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLDivElement>(null);
  useClickOutside(menuRef, () => setMenu(false), menu);
  const [symbols, setSymbols] = useState<SymbolInfo[]>(() => (peek<SymbolInfo[]>("/api/symbols") ?? []).filter((x) => x.quote === "USDT"));
  const [market, setMarket] = useState<MarketRow[]>(() => peek<{ rows: MarketRow[] }>("/api/market")?.rows ?? []);
  const [q, setQ] = useState("");
  useClickOutside(searchRef, () => setQ(""), q !== "");

  const load = () => {
    api.accounts().then((a) => {
      setAccounts(a);
      const id = accountId != null && a.some((x) => x.id === accountId) ? accountId : a[0]?.id ?? null;
      if (id !== accountId) setAccountId(id);
      if (id != null) api.prop(id, range.toLowerCase()).then(setData).catch((e) => setError(String(e.message)));
    }).catch((e) => setError(String(e.message)));
  };
  useEffect(() => { load(); const t = setInterval(load, 15_000); return () => clearInterval(t); }, [accountId, range]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    api.symbols().then((s) => setSymbols(s.filter((x) => x.quote === "USDT"))).catch(() => {});
    const loadMarket = () => api.market().then((r) => setMarket(r.rows)).catch(() => {});
    loadMarket();
    const t = setInterval(loadMarket, 30_000);
    return () => clearInterval(t);
  }, []);

  const movers = useMemo(() => market.filter((r) => r.quoteVolume >= 10_000_000).sort((a, b) => Math.abs(b.changePct) - Math.abs(a.changePct)).slice(0, 8), [market]);
  const matches = useMemo(() => {
    const needle = q.trim().toUpperCase();
    if (!needle) return [];
    return symbols.filter((s) => s.symbol.includes(needle)).slice(0, 8);
  }, [q, symbols]);

  const act = async (id: number, action: "open" | "cancel") => {
    try { await api.tradeAction(id, action); load(); } catch (e) { setError(String((e as Error).message)); }
  };
  const researchPosition = async (id: number) => {
    setResearchingId(id); setError(null);
    try { await api.researchPosition(id); load(); } catch (e) { setError(String((e as Error).message)); }
    setResearchingId(null);
  };
  // Comparison curves: one request per selected account, capped at MAX_COMPARE.
  useEffect(() => {
    if (!compare || compareIds.length === 0) { setCompareSeries([]); return; }
    let cancelled = false;
    Promise.all(compareIds.slice(0, MAX_COMPARE).map((id) => api.prop(id, range.toLowerCase()).then((r) => ({ name: r.account.name, pts: r.equity, start: r.account.size })).catch(() => null)))
      .then((rows) => { if (!cancelled) setCompareSeries(rows.filter((r): r is NonNullable<typeof r> => !!r)); });
    return () => { cancelled = true; };
  }, [compare, compareIds, range, data?.account.equity]); // eslint-disable-line react-hooks/exhaustive-deps

  const a = data?.account;
  const pnl = a ? a.equity - a.size : 0;
  return (
    <div className="p-4 flex flex-col gap-4 max-w-6xl">
      <div className="flex flex-wrap items-center gap-3">
        <select value={accountId ?? ""} onChange={(e) => setAccountId(Number(e.target.value))} className="bg-zinc-900 border border-zinc-800 rounded px-3 py-1.5 text-[17px] font-semibold">
          {accounts.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
        <div className="relative" ref={menuRef}>
          <button onClick={() => setMenu((m) => !m)} className="px-3 py-1.5 rounded border border-zinc-800 text-zinc-400 hover:text-zinc-200 text-sm tracking-widest" title="Account actions">•••</button>
          {menu && accountId != null && (
            <div className="absolute z-20 mt-1 w-48 rounded border border-zinc-700 bg-zinc-900 shadow-xl text-sm py-1">
              <button onClick={() => { setMenu(false); setCreating(true); }} className="block w-full text-left px-3 py-1.5 hover:bg-zinc-800 text-zinc-200">Create Account</button>
              <button onClick={async () => { setMenu(false); const name = window.prompt("Account name", a?.name ?? ""); if (name && name.trim()) { try { await api.renameAccount(accountId, name.trim()); load(); } catch (e) { setError(String((e as Error).message)); } } }} className="block w-full text-left px-3 py-1.5 hover:bg-zinc-800 text-zinc-200">Rename</button>
              <a href={api.ledgerUrl(accountId)} download onClick={() => setMenu(false)} className="block px-3 py-1.5 hover:bg-zinc-800 text-zinc-200" title="Every trade with costs, thesis and your notes, as CSV">Export Ledger</a>
              <button onClick={async () => { setMenu(false); if (!a) return; if (window.confirm(`Delete "${a.name}" and all of its trades and history? This cannot be undone.`)) { try { await api.deleteAccount(accountId); setAccountId(null); load(); } catch (e) { setError(String((e as Error).message)); } } }} className="block w-full text-left px-3 py-1.5 hover:bg-zinc-800 text-red-300">Delete Account</button>
            </div>
          )}
        </div>
        {error && <span className="text-red-400 text-sm">{error}</span>}
      </div>
      {creating && (
        <NewAccountModal form={form} setForm={setForm} onClose={() => setCreating(false)}
          onCreate={async () => { try { const acc = await api.createAccount(form); setCreating(false); setAccountId(acc.id); setCelebrate(acc.id); load(); } catch (e) { setError(String((e as Error).message)); } }} />
      )}

      {a && (
        <section className={`rounded-lg border p-5 ${a.breached ? "border-red-700 bg-red-950/20" : "border-zinc-700 bg-zinc-900/40"}`}>
          <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
            <div>
              <div className="text-sm uppercase tracking-wide text-zinc-500">{a.name}</div>
              <div className="text-5xl font-semibold num tracking-tight"><CountUp value={a.equity} animate={celebrate === a.id} onDone={() => setCelebrate(null)} /></div>
            </div>
            <div className={`num text-2xl pb-1 ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>{pnl >= 0 ? "+" : "−"}${Math.abs(pnl).toFixed(2)} <span className="text-xl">({fmtPct(a.returnPct)})</span></div>
            <div className="ml-auto flex items-center gap-2 self-center">
              <button onClick={() => { setCompare(!compare); if (!compare && compareIds.length === 0) setCompareIds([a.id]); }} className={`px-3 py-1 text-sm rounded border ${compare ? "border-zinc-500 text-zinc-100" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"}`} title="Overlay several accounts as percent return">Compare</button>
              <div className="inline-flex rounded border border-zinc-700 overflow-hidden">
                {RANGES.map((r) => <button key={r} onClick={() => setRange(r)} className={`px-3 py-1 text-sm uppercase ${range === r ? "bg-zinc-700 text-white" : "text-zinc-400 hover:bg-zinc-800"}`}>{r}</button>)}
              </div>
            </div>
          </div>
          {compare && (
            <div className="flex flex-wrap items-center gap-2 mt-3 text-sm">
              {accounts.map((x) => {
                const on = compareIds.includes(x.id);
                const full = !on && compareIds.length >= MAX_COMPARE;
                return <button key={x.id} disabled={full} onClick={() => setCompareIds(on ? compareIds.filter((i) => i !== x.id) : [...compareIds, x.id])} className={`px-2 py-0.5 rounded border ${on ? "border-zinc-400 text-zinc-100" : "border-zinc-800 text-zinc-500"} disabled:opacity-40`}>{x.name}</button>;
              })}
              <span className="text-zinc-500">{compareIds.length} of {MAX_COMPARE} max</span>
            </div>
          )}
          <div className="mt-3">{compare ? <CompareChart series={compareSeries} /> : <EquityChart pts={data!.equity} start={a.size} />}</div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-x-6 gap-y-2 mt-3 num text-[15px]">
            <div><div className="text-sm text-zinc-500">Target</div><div>${fmtCompact(a.targetUsd)} <span className="text-zinc-500">· {Math.max(0, a.targetProgressPct).toFixed(0)}% there</span></div></div>
            <div><Term k="drawdown" value={a.drawdownPct}><div className="text-sm text-zinc-500 cursor-help">Drawdown remaining</div></Term><div>${Math.max(0, (a.max_dd_pct - a.drawdownPct) / 100 * a.size).toFixed(0)} <span className="text-zinc-500">of ${(a.max_dd_pct / 100 * a.size).toFixed(0)}</span></div></div>
            <div><Term k="dailyLoss"><div className="text-sm text-zinc-500 cursor-help">Daily loss remaining</div></Term><div>${a.dailyLossRemainingUsd.toFixed(0)} <span className="text-zinc-500">of ${(a.daily_loss_pct / 100 * a.size).toFixed(0)}</span></div></div>
            <div><Term k="openRisk" value={a.openRiskPct}><div className="text-sm text-zinc-500 cursor-help">Open risk</div></Term><div>{a.openRiskPct.toFixed(2)}% <span className="text-zinc-500">of 3% cap</span></div></div>
            <div><div className="text-sm text-zinc-500">Open P&L</div><div className={a.unrealized >= 0 ? "text-emerald-400" : "text-red-400"}>{a.unrealized >= 0 ? "+" : ""}${a.unrealized.toFixed(0)} <span className="text-zinc-500">· {a.counts.open} open · {a.counts.waiting} waiting · {a.counts.ready} ready</span></div></div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mt-4">
            <Bar used={((a.size * a.daily_loss_pct / 100 - a.dailyLossRemainingUsd) / (a.size * a.daily_loss_pct / 100)) * 100} label="Daily loss budget used" />
            <Bar used={(a.drawdownPct / a.max_dd_pct) * 100} label={`Drawdown ${a.drawdownPct.toFixed(2)}% of ${a.max_dd_pct}%`} />
            <Bar used={(a.openRiskPct / 3) * 100} label="Open risk used" />
            <Bar used={Math.max(0, a.targetProgressPct)} label={`Target progress · ${a.target_pct}% goal`} />
          </div>
          {a.breached && <div className="mt-3 text-red-400 font-semibold">RULE BREACHED · a real challenge would be over</div>}
        </section>
      )}

      {/* Trade anything: search, or pick from what is moving. */}
      <section className="rounded border border-zinc-800 p-4">
        <div className="flex flex-wrap items-center gap-3 mb-3">
          <div className="text-sm uppercase tracking-wide text-zinc-500">Find a Market</div>
          <div className="relative" ref={searchRef}>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="BTC, SOL, INJ…" className="bg-zinc-900 border border-zinc-800 rounded px-3 py-1.5 w-64 outline-none focus:border-zinc-600" />
            {matches.length > 0 && (
              <ul className="absolute z-20 mt-1 w-72 rounded border border-zinc-800 bg-zinc-900 shadow-xl">
                {matches.map((s) => (
                  <li key={s.symbol} className="flex items-center justify-between px-3 py-1.5 hover:bg-zinc-800">
                    <span>{s.symbol}{s.tracked && <span className="ml-2 text-xs text-sky-400">tracked</span>}</span>
                    <span className="flex gap-1">
                      <button onClick={() => { setQ(""); api.scan(s.symbol).then(() => navigate("analysis", { symbol: s.symbol })); }} className="px-2 py-0.5 rounded border border-zinc-700 text-zinc-400 text-xs uppercase">Analyze</button>
                      <button onClick={() => { setQ(""); setTicket(s.symbol); }} className="px-2 py-0.5 rounded bg-zinc-100 text-zinc-900 text-xs font-semibold uppercase">Trade</button>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <span className="text-sm text-zinc-500">Any USDT pair on the exchange. Untracked coins start streaming when you open a ticket.</span>
        </div>
        <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">Top Movers · Liquid Pairs</div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {movers.map((r) => (
            <div key={r.symbol} className="flex items-center gap-2 rounded border border-zinc-800/80 px-3 py-2">
              <button onClick={() => navigate("charts", { symbol: r.symbol })} className="font-semibold hover:underline">{r.symbol.replace("USDT", "")}</button>
              <span className={`num text-sm ${r.changePct >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtPct(r.changePct, 1)}</span>
              <span className="ml-auto flex gap-1">
                <button onClick={() => api.scan(r.symbol).then(() => navigate("analysis", { symbol: r.symbol }))} className="px-1.5 py-0.5 rounded border border-zinc-700 text-zinc-400 text-xs uppercase" title="Run the scanner on it and open Analysis">Analyze</button>
                <button onClick={() => setTicket(r.symbol)} className="px-2 py-0.5 rounded bg-zinc-100 text-zinc-900 text-xs font-semibold uppercase">Trade</button>
              </span>
            </div>
          ))}
          {movers.length === 0 && <div className="text-zinc-500 text-sm">Market data loading…</div>}
        </div>
      </section>

      {data && data.open.length > 0 && (
        <section className="rounded border border-zinc-800 p-4">
          <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">Open Positions</div>
          <div className="flex flex-col gap-2">
            {data.open.map((t) => {
              const st = STATUS[t.status] ?? STATUS.hold;
              const needs = t.status !== "hold";
              return (
                <div key={t.id} className={`rounded border p-4 ${needs ? "border-amber-800/60 bg-amber-950/10" : "border-zinc-800"}`}>
                  <div className="flex flex-wrap items-center gap-3 text-[17px]">
                    <button onClick={() => navigate("charts", { symbol: t.symbol })} className="font-semibold hover:underline">{t.symbol}</button>
                    <span className={`uppercase text-sm ${t.side === "long" ? "text-emerald-400" : "text-red-400"}`}>{t.side.toUpperCase()}</span>
                    <span className={`num ${(t.unrealizedUsd ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>{(t.unrealizedUsd ?? 0) >= 0 ? "+" : ""}${(t.unrealizedUsd ?? 0).toFixed(0)} · {t.unrealizedR != null ? <Term k="r" value={t.unrealizedR} ctx={{ riskUsd: t.risk_usd }}><span className="cursor-help">{`${t.unrealizedR >= 0 ? "+" : ""}${t.unrealizedR.toFixed(2)}R`}</span></Term> : ""}</span>
                    <span className={`text-sm font-semibold tracking-wide ${st.tone}`}>{st.label}</span>
                    <span className="text-sm text-zinc-500 num">{t.opened_at ? `${Math.floor((Date.now() / 1000 - t.opened_at) / 3600)}h held` : ""}</span>
                    {t.payload.againstAdvice && <span className="text-xs uppercase text-amber-400/80" title="Opened against Wick's advice">Against advice</span>}
                    <div className="ml-auto flex gap-2">
                      <button onClick={() => researchPosition(t.id)} disabled={researchingId === t.id} className="px-3 py-1.5 rounded border border-zinc-700 text-zinc-300 text-sm uppercase disabled:opacity-50" title="One model call: what changed since entry and what to do now. Advisory only.">{researchingId === t.id ? "Researching…" : t.payload.positionResearch ? "Refresh Research" : "Research Position"}</button>
                      <button onClick={() => setClosing(t)} className={`px-3 py-1.5 rounded text-sm uppercase ${needs ? "bg-zinc-100 text-zinc-900 font-semibold" : "border border-zinc-700 text-zinc-400"}`}>Review & Close</button>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-x-4 text-sm num text-zinc-400 mt-1">
                    <span>entry {fmtPrice(t.entry)}</span><span>now {fmtPrice(t.currentPrice)}</span><span>stop {fmtPrice(t.stop)}</span><span>target {t.target != null ? fmtPrice(t.target) : "none"}</span><span>size ${fmtCompact(t.size_usd)} · risk ${t.risk_usd.toFixed(0)}</span>
                  </div>
                  <div className="text-[15px] text-zinc-300 mt-1">{t.status_note}</div>
                  {t.changes && <div className="text-sm text-zinc-500">{t.changes}</div>}
                  {t.payload.thesis && <div className="text-sm text-zinc-500 mt-0.5">Thesis: {t.payload.thesis}</div>}
                  {t.payload.partials && t.payload.partials.length > 0 && <div className="text-sm text-zinc-500 mt-0.5">Reduced {t.payload.partials.length}× · realized {t.payload.partials.reduce((s, x) => s + x.pnl, 0) >= 0 ? "+" : "−"}${Math.abs(t.payload.partials.reduce((s, x) => s + x.pnl, 0)).toFixed(0)} so far</div>}
                  {t.payload.positionResearch && <PositionAdvice r={t.payload.positionResearch} />}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {data && data.waiting.length > 0 && (
        <section className="rounded border border-zinc-800 p-4">
          <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">Waiting for Entry</div>
          {data.waiting.map((t) => (
            <div key={t.id} className={`flex flex-wrap items-center gap-3 text-sm py-2 border-t border-zinc-900 ${t.state === "ready" ? "text-sky-200" : "text-zinc-400"}`}>
              <span className="font-semibold text-zinc-200 text-[15px]">{t.symbol}</span><span className="uppercase">{t.side.toUpperCase()}</span>
              <span className={`font-semibold ${t.state === "ready" ? "text-sky-300" : ""}`}>{t.state === "ready" ? "TRADE READY" : `waiting · ${t.trigger_kind}`}</span>
              <span className="num">trigger {fmtPrice(t.plan_entry)} · stop {fmtPrice(t.stop)} · target {t.target != null ? fmtPrice(t.target) : "none"} · ${fmtCompact(t.size_usd)}</span>
              <span className="text-zinc-500">{t.status_note}</span>
              <div className="ml-auto flex gap-2">
                <button onClick={() => act(t.id, "open")} className={`px-3 py-1.5 rounded text-sm font-semibold uppercase ${t.state === "ready" ? "bg-zinc-100 text-zinc-900" : "border border-zinc-700 text-zinc-400"}`}>{t.state === "ready" ? "Open position" : "Open at market"}</button>
                <button onClick={() => act(t.id, "cancel")} className="px-2 py-1.5 rounded border border-zinc-800 text-zinc-500 text-sm">Cancel</button>
              </div>
            </div>
          ))}
        </section>
      )}

      {data && data.closed.length > 0 && (
        <section className="rounded border border-zinc-800 p-4">
          <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">History</div>
          <table className="w-full text-sm num">
            <thead className="text-zinc-500"><tr><th className="text-left font-normal py-1">Opened</th><th className="text-left font-normal">Symbol</th><th className="text-left font-normal">Side</th><th className="text-right font-normal">Entry</th><th className="text-right font-normal">Exit</th><th className="text-left font-normal pl-3">Reason</th><th className="text-right font-normal">P&L</th><th className="text-right font-normal">R</th><th className="text-left font-normal pl-3">Notes</th><th></th></tr></thead>
            <tbody>
              {data.closed.slice(0, 60).map((t) => (
                <tr key={t.id} className="border-t border-zinc-900 text-zinc-400">
                  <td className="py-1.5">{fmtTime(t.opened_at ?? t.created_at).slice(0, 16)}</td><td className="text-zinc-200">{t.symbol}</td><td className="uppercase">{t.side}</td>
                  <td className="text-right">{fmtPrice(t.entry)}</td><td className="text-right">{fmtPrice(t.exit)}</td><td className="pl-3">{t.exit_reason === "stop" ? "STOP FILLED" : t.exit_reason === "manual" ? "Closed by you" : t.exit_reason === "partial" ? "Partial close" : t.exit_reason === "expired" ? "Expired" : t.exit_reason === "cancelled" ? "Cancelled" : t.exit_reason}</td>
                  <td className={`text-right ${(t.pnl_usd ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>{t.pnl_usd != null ? `${t.pnl_usd >= 0 ? "+" : ""}${t.pnl_usd.toFixed(0)}` : ""}</td>
                  <td className="text-right">{t.r_multiple != null ? `${t.r_multiple >= 0 ? "+" : ""}${t.r_multiple.toFixed(2)}` : ""}</td>
                  <td className="pl-3 max-w-xs truncate text-zinc-500" title={t.notes ?? ""}>{t.notes || ""}</td>
                  <td className="text-right">{t.state === "closed" && <button onClick={() => setReview(t)} className="text-zinc-500 hover:text-zinc-200">Review</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {a && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <section className="rounded border border-zinc-800 p-4 text-[15px]">
            <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">Track Record</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 num">
              <span className="text-zinc-400">Closed trades</span><span className="text-right">{a.stats.n}</span>
              <span className="text-zinc-400">Win rate</span><span className="text-right">{a.stats.winRate != null ? `${(a.stats.winRate * 100).toFixed(0)}%` : "–"}</span>
              <span className="text-zinc-400">Expectancy</span><span className="text-right">{a.stats.expectancyR != null ? `${a.stats.expectancyR >= 0 ? "+" : ""}${a.stats.expectancyR.toFixed(2)}R` : "–"}</span>
              <span className="text-zinc-400">Max drawdown</span><span className="text-right">{fmtNum(a.stats.maxDrawdownPct, 2)}%</span>
            </div>
          </section>
          <section className="rounded border border-zinc-800 p-4 text-[15px]">
            <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">By Shape at Entry</div>
            {Object.keys(a.stats.byShape).length === 0 ? <div className="text-zinc-500">Nothing closed yet.</div> : Object.entries(a.stats.byShape).map(([k, v]) => (
              <div key={k} className="flex justify-between num py-1"><span className="text-zinc-400 capitalize">{k.replace(/_/g, " ")}</span><span>{v.n} · {v.sumR >= 0 ? "+" : ""}{v.sumR.toFixed(1)}R · {((v.wins / v.n) * 100).toFixed(0)}%</span></div>
            ))}
          </section>
          <section className="rounded border border-zinc-800 p-4 text-[15px]">
            <div className="text-sm uppercase tracking-wide text-zinc-500 mb-2">You vs. the Judges (R)</div>
            <div className="flex justify-between num py-1"><span className="text-zinc-200">You ({a.name})</span><span>{a.stats.n} · {a.stats.expectancyR != null ? `${(a.stats.expectancyR * a.stats.n) >= 0 ? "+" : ""}${(a.stats.expectancyR * a.stats.n).toFixed(1)}R` : "–"}</span></div>
            {data && Object.entries(data.judges).map(([k, v]) => (
              <div key={k} className="flex justify-between num py-1"><span className="text-zinc-400">{k === "rules" ? "Rules judge" : k === "model" ? "Model judge" : k}</span><span>{v.n} · {v.sumR >= 0 ? "+" : ""}{v.sumR.toFixed(1)}R · {v.n ? ((v.wins / v.n) * 100).toFixed(0) : 0}%</span></div>
            ))}
            <div className="text-sm text-zinc-500 mt-1">Judges' calls are scored at a fixed $1,000 from the recommendation log; your trades are sized by your account rules.</div>
          </section>
        </div>
      )}

      {review && <ReviewModal t={review} onClose={() => setReview(null)} />}
      {closing && <CloseModal t={closing} onClose={() => setClosing(null)} onDone={(closed) => { setClosing(null); load(); if (closed) setReview(closed); }} />}
      {ticket && <TradeTicket symbol={ticket} onClose={() => setTicket(null)} onDone={() => { setTicket(null); load(); }} />}
      {data && data.open.length === 0 && data.waiting.length === 0 && data.closed.length === 0 && (
        <div className="text-[15px] text-zinc-500">No trades yet. Search a market above, pick a mover, or go to Analysis and let Wick find one.</div>
      )}
    </div>
  );
}

const ADVICE: Record<PositionResearch["action"], { label: string; tone: string }> = {
  hold: { label: "HOLD", tone: "text-emerald-300" },
  watch_closely: { label: "WATCH CLOSELY", tone: "text-amber-200" },
  reduce: { label: "REDUCE", tone: "text-amber-300" },
  consider_exit: { label: "CONSIDER EXIT", tone: "text-red-300" },
};

/** What the model thinks the holder should do, action first, then why, then the detail. */
function PositionAdvice({ r }: { r: PositionResearch }) {
  const [open, setOpen] = useState(false);
  const a = ADVICE[r.action] ?? ADVICE.hold;
  const age = (Date.now() - r.time) / 60000;
  return (
    <div className="mt-2 rounded bg-zinc-900/60 p-2 text-sm">
      <div className="text-xs uppercase tracking-wide text-zinc-500">AI position review · {age < 60 ? `${Math.max(1, Math.round(age))}m ago` : `${(age / 60).toFixed(1)}h ago`} · advisory only</div>
      <div className="mt-0.5"><span className={`font-semibold ${a.tone}`}>{a.label}</span> — <span className="text-zinc-200">{r.reason}</span></div>
      <div className="text-zinc-400 mt-0.5">What changed: {r.whatChanged}</div>
      <button onClick={() => setOpen((o) => !o)} className="text-xs text-zinc-500 hover:text-zinc-300 mt-1">{open ? "▾ Less" : "▸ More"}</button>
      {open && (
        <div className="mt-1 text-zinc-400">
          <div>{r.summary}</div>
          {r.risks.length > 0 && <div className="text-amber-300/80 mt-1">Risks: {r.risks.join("; ")}</div>}
          {r.drivers.length > 0 && <div className="mt-1">{r.drivers.slice(0, 3).map((d, i) => <span key={i}>{d.text}{d.url && <a href={d.url} target="_blank" rel="noreferrer" className="text-sky-400 ml-1">↗</a>}{i < Math.min(3, r.drivers.length) - 1 ? " · " : ""}</span>)}</div>}
          <div className="text-xs text-zinc-500 mt-1">{r.confidence} confidence · {r.model}</div>
        </div>
      )}
    </div>
  );
}

/** Closing is never one click: choose how much, see what it realizes, then confirm. */
function CloseModal({ t, onClose, onDone }: { t: Trade; onClose: () => void; onDone: (closed: Trade | null) => void }) {
  const [fraction, setFraction] = useState(1);
  const [amount, setAmount] = useState(String(Math.round(t.size_usd)));
  const [preview, setPreview] = useState<ClosePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const h = setTimeout(() => api.closePreview(t.id, fraction).then(setPreview).catch((e) => setError(String(e.message))), 250);
    return () => clearTimeout(h);
  }, [t.id, fraction]);
  const pick = (f: number) => { setFraction(f); setAmount(String(Math.round(t.size_usd * f))); };
  const typed = (v: string) => { setAmount(v); const n = Number(v.replace(/[^0-9.]/g, "")); if (n > 0) setFraction(Math.min(1, n / t.size_usd)); };
  const full = fraction >= 0.999;
  const confirm = async () => {
    setBusy(true); setError(null);
    try { const res = await api.closeTrade(t.id, fraction); onDone(full ? res : null); } catch (e) { setError(String((e as Error).message)); setBusy(false); }
  };
  const money = (v: number) => `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`;
  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div className="w-full max-w-md max-h-[92vh] overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-950 p-5 text-[15px] shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-baseline justify-between mb-3">
          <div><span className="text-[13px] uppercase tracking-wide text-zinc-500 mr-2">Close position</span><span className="text-xl font-semibold">{t.symbol}</span><span className={`ml-2 uppercase text-sm ${t.side === "long" ? "text-emerald-400" : "text-red-400"}`}>{t.side}</span></div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-200 text-xl leading-none">✕</button>
        </div>
        <div className="text-sm text-zinc-400 mb-3">Current position <span className="num text-zinc-100">${fmtCompact(t.size_usd)}</span> · entry {fmtPrice(t.entry)} · stop {fmtPrice(t.stop)}</div>
        <div className="flex gap-2 mb-2">
          {[0.25, 0.5, 0.75, 1].map((f) => <button key={f} onClick={() => pick(f)} className={`flex-1 py-1.5 rounded border text-sm ${Math.abs(fraction - f) < 0.005 ? "border-zinc-300 text-zinc-100" : "border-zinc-700 text-zinc-400 hover:text-zinc-200"}`}>{f * 100}%</button>)}
        </div>
        <label className="flex items-center gap-2 mb-3 text-sm text-zinc-400">Amount <span className="text-zinc-500">$</span><input value={amount} onChange={(e) => typed(e.target.value)} className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 w-32 num text-zinc-100" /></label>
        {preview ? (
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 num mb-3">
            <span className="text-zinc-400">Closing</span><span className="text-right">${preview.closeUsd.toFixed(0)} at {fmtPrice(preview.price)}</span>
            <span className="text-zinc-400">Estimated realized P&L</span><span className={`text-right font-semibold ${preview.realizedUsd >= 0 ? "text-emerald-400" : "text-red-400"}`}>{money(preview.realizedUsd)}{preview.realizedR != null ? ` · ${preview.realizedR >= 0 ? "+" : ""}${preview.realizedR.toFixed(2)}R` : ""}</span>
            <span className="text-zinc-400">Fees and funding</span><span className="text-right text-zinc-300">{money(preview.costs.fees + preview.costs.funding)}</span>
            <span className="text-zinc-400">Remaining exposure</span><span className="text-right">{full ? "none" : `$${preview.remainingUsd.toFixed(0)}`}</span>
            <span className="text-zinc-400">Remaining risk at stop</span><span className="text-right">{full ? "none" : `$${preview.remainingRiskUsd.toFixed(0)}`}</span>
          </div>
        ) : <div className="text-zinc-500 mb-3">{error ?? "Pricing…"}</div>}
        {error && preview && <div className="text-sm text-red-400 mb-2">{error}</div>}
        <div className="flex gap-2">
          <button disabled={busy || !preview || fraction <= 0} onClick={confirm} className="flex-1 py-2.5 rounded bg-zinc-100 text-zinc-900 font-semibold uppercase tracking-wide disabled:opacity-40">{busy ? "Closing…" : full ? "Confirm Close Position" : `Confirm Partial Close · ${Math.round(fraction * 100)}%`}</button>
          <button onClick={onClose} className="px-4 py-2 rounded border border-zinc-700 text-zinc-400">Cancel</button>
        </div>
      </div>
    </div>
  );
}

/** Close / review screen: the numbers, the thesis, and the path the price took. */
function ReviewModal({ t, onClose }: { t: Trade; onClose: () => void }) {
  const [closes, setCloses] = useState<{ time: number; close: number }[]>([]);
  const [notes, setNotes] = useState(t.notes ?? "");
  const [saved, setSaved] = useState(false);
  const costs = t.payload.costs;
  useEffect(() => {
    api.candles(t.symbol, "1h", 400).then((r) => {
      const from = (t.opened_at ?? t.created_at) - 24 * 3600, to = (t.closed_at ?? Date.now() / 1000) + 12 * 3600;
      setCloses(r.candles.filter((c) => c.time >= from && c.time <= to).map((c) => ({ time: c.time, close: c.close })));
    }).catch(() => {});
  }, [t]);
  // Rows from the list API carry seconds; a row returned straight from a close carries milliseconds.
  const secs = (v: number | null) => (v == null ? null : v > 1e11 ? Math.floor(v / 1000) : v);
  const openedS = secs(t.opened_at) ?? secs(t.created_at) ?? 0, closedS = secs(t.closed_at);
  const held = openedS && closedS ? closedS - openedS : 0;
  const w = 560, h = 160;
  const ys = closes.map((c) => c.close).concat([t.stop, t.entry ?? t.plan_entry, t.exit ?? 0].filter((x) => x > 0));
  const min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1;
  const x = (time: number) => closes.length > 1 ? ((time - closes[0].time) / (closes[closes.length - 1].time - closes[0].time)) * w : 0;
  const y = (v: number) => h - ((v - min) / span) * (h - 8) - 4;
  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div className="w-full max-w-2xl max-h-[92vh] overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-950 p-5" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-baseline gap-3 mb-3">
          <span className="text-sm uppercase tracking-wide text-zinc-500">Trade closed</span>
          <span className="text-xl font-semibold">{t.symbol}</span><span className={`uppercase text-sm ${t.side === "long" ? "text-emerald-400" : "text-red-400"}`}>{t.side.toUpperCase()}</span>
          <span className={`ml-auto text-3xl font-semibold num ${(t.pnl_usd ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>{(t.pnl_usd ?? 0) >= 0 ? "+" : ""}${(t.pnl_usd ?? 0).toFixed(0)} · {t.r_multiple != null ? `${t.r_multiple >= 0 ? "+" : ""}${t.r_multiple.toFixed(2)}R` : ""}</span>
        </div>
        {closes.length > 1 && (
          <svg viewBox={`0 0 ${w} ${h}`} className="block w-full mb-3">
            <polyline points={closes.map((c) => `${x(c.time)},${y(c.close)}`).join(" ")} fill="none" stroke="#a1a1aa" strokeWidth="1" />
            <line x1={0} x2={w} y1={y(t.stop)} y2={y(t.stop)} stroke="#f87171" strokeDasharray="3 3" />
            {t.target != null && <line x1={0} x2={w} y1={y(t.target)} y2={y(t.target)} stroke="#34d399" strokeDasharray="3 3" />}
            {t.opened_at && t.entry && <circle cx={x(t.opened_at)} cy={y(t.entry)} r={4} fill="#38bdf8" />}
            {t.closed_at && t.exit && <circle cx={x(t.closed_at)} cy={y(t.exit)} r={4} fill={(t.pnl_usd ?? 0) >= 0 ? "#34d399" : "#f87171"} />}
          </svg>
        )}
        <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-[15px] num">
          <span className="text-zinc-400">Entry → exit</span><span className="text-right">{fmtPrice(t.entry)} → {fmtPrice(t.exit)}</span>
          <span className="text-zinc-400">Exit reason</span><span className="text-right">{t.exit_reason === "stop" ? "Stop filled at the planned level" : t.exit_reason === "manual" ? "Closed by you" : t.exit_reason === "partial" ? "Partial close by you" : t.exit_reason}</span>
          <span className="text-zinc-400">Held</span><span className="text-right">{Math.floor(held / 3600)}h {Math.floor((held % 3600) / 60)}m</span>
          <span className="text-zinc-400">Plan</span><span className="text-right">Stop {fmtPrice(t.stop)} · target {t.target != null ? fmtPrice(t.target) : "none"}{t.payload.plan?.rr != null ? ` · R:R ${t.payload.plan.rr.toFixed(2)}` : ""} · planner {t.planner_version}</span>
          <span className="text-zinc-400">Shape at entry</span><span className="text-right capitalize">{t.payload.shapeAtEntry?.replace(/_/g, " ") ?? "–"}</span>
        </div>
        {costs && (
          <div className="mt-3 rounded border border-zinc-800 p-3 grid grid-cols-2 gap-x-6 gap-y-1 text-[15px] num">
            <span className="text-zinc-400">Gross P&L</span><span className={`text-right ${costs.gross >= 0 ? "text-emerald-400" : "text-red-400"}`}>{costs.gross >= 0 ? "+" : "−"}${Math.abs(costs.gross).toFixed(2)}</span>
            <span className="text-zinc-400">Trading fees (0.10% each way)</span><span className="text-right text-zinc-300">−${Math.abs(costs.fees).toFixed(2)}</span>
            <span className="text-zinc-400">Funding ({costs.hoursHeld.toFixed(1)}h held)</span><span className="text-right text-zinc-300">{costs.funding >= 0 ? "+" : "−"}${Math.abs(costs.funding).toFixed(2)}</span>
            <span className="text-zinc-400">Slippage</span><span className="text-right text-zinc-500">in the entry price</span>
            <span className="text-zinc-200 font-semibold border-t border-zinc-800 pt-1">Net P&L</span><span className={`text-right font-semibold border-t border-zinc-800 pt-1 ${costs.net >= 0 ? "text-emerald-400" : "text-red-400"}`}>{costs.net >= 0 ? "+" : "−"}${Math.abs(costs.net).toFixed(2)}</span>
          </div>
        )}
        {t.payload.thesis && <div className="text-sm text-zinc-400 mt-3"><span className="text-zinc-500">Original thesis: </span>{t.payload.thesis}</div>}
        <div className="text-[15px] text-zinc-300 mt-3">
          {(t.pnl_usd ?? 0) >= 0 ? (t.exit_reason === "manual" ? "Closed in profit by your decision." : "Played out as planned.")
            : t.exit_reason === "stop" ? "Thesis invalidated at the planned level. Risk plan followed." : "Closed at a loss by your decision, ahead of the stop."}
        </div>
        <label className="block mt-3">
          <span className="text-sm text-zinc-500">Your notes (go into the ledger export)</span>
          <textarea value={notes} onChange={(e) => { setNotes(e.target.value); setSaved(false); }} rows={3} placeholder="What you saw, why you took it, what you would do differently…" className="mt-1 w-full bg-zinc-900 border border-zinc-800 rounded px-3 py-2 text-[15px] outline-none focus:border-zinc-600" />
        </label>
        <div className="flex gap-2 mt-3">
          <button onClick={async () => { try { await api.setNotes(t.id, notes); setSaved(true); } catch { setSaved(false); } }} className="px-5 py-2 rounded bg-zinc-100 text-zinc-900 font-semibold">{saved ? "Saved" : "Save notes"}</button>
          <button onClick={onClose} className="px-5 py-2 rounded border border-zinc-700 text-zinc-300">Done</button>
        </div>
      </div>
    </div>
  );
}
