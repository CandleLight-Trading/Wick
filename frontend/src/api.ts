import type { Account, AnalysisPayload, ClosePreview, ConditionEvent, ContextData, Interval, MarketRow, ModelAnalysis, PaperPayload, PlanResponse, PositionResearch, PropPayload, Status, SymbolInfo, Trade, WireCandle } from "./types";

/** A 401 anywhere means the session is gone: the app shows the login screen. */
export const UNAUTHORIZED = "wick:unauthorized";

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (r.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED));
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const api = {
  auth: () => get<{ enabled: boolean; loggedIn: boolean }>("/api/auth"),
  login: (password: string) => post<{ ok: boolean }>("/api/login", { password }),
  logout: (everywhere = false) => post<{ ok: boolean }>(`/api/logout?everywhere=${everywhere}`),
  symbols: () => get<SymbolInfo[]>("/api/symbols"),
  tracked: () => get<string[]>("/api/tracked"),
  setTracked: async (symbols: string[]) => {
    const r = await fetch("/api/tracked", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json() as Promise<string[]>;
  },
  candles: (symbol: string, interval: Interval, limit = 500) =>
    get<{ symbol: string; interval: Interval; tracked: boolean; candles: WireCandle[] }>(
      `/api/candles?symbol=${symbol}&interval=${interval}&limit=${limit}`,
    ),
  market: () => get<{ quote: string; rows: MarketRow[] }>("/api/market"),
  context: (symbol: string) => get<ContextData>(`/api/context?symbol=${symbol}`),
  conditionLog: (symbol: string) => get<ConditionEvent[]>(`/api/condition_log?symbol=${symbol}&limit=300`),
  status: () => get<Status>("/api/status"),
  analysis: () => get<AnalysisPayload>("/api/analysis"),
  paper: () => get<PaperPayload>("/api/paper"),
  runAnalysis: async (symbol: string) => {
    const r = await fetch(`/api/analysis/run?symbol=${symbol}`, { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    return r.json() as Promise<ModelAnalysis>;
  },
  // Phase 5: setups, accounts, trades
  researchSetup: (id: number) => post(`/api/setups/${id}/research`),
  passSetup: (id: number) => post(`/api/setups/${id}/pass`),
  pinSetup: (symbol: string) => post(`/api/setups/pin?symbol=${symbol}`),
  clearResearch: (id?: number) => post(id != null ? `/api/setups/${id}/clear_research` : "/api/analysis/clear_research"),
  resetWorkspace: () => post("/api/analysis/reset_workspace"),
  plan: (q: { setupId?: number; symbol?: string; side?: string; accountId: number; risk: string }) => {
    const qs = new URLSearchParams({ account_id: String(q.accountId), risk_profile: q.risk });
    if (q.setupId != null) qs.set("setup_id", String(q.setupId));
    else { qs.set("symbol", q.symbol ?? ""); qs.set("side", q.side ?? "long"); }
    return get<PlanResponse>(`/api/plan?${qs}`);
  },
  scan: (symbol?: string) => post<{ ok: boolean; lastScan: number }>(`/api/analysis/scan${symbol ? `?symbol=${symbol}` : ""}`),
  accounts: () => get<Account[]>("/api/accounts"),
  createAccount: (body: { name: string; size: number; daily_loss_pct: number; max_dd_pct: number; target_pct: number }) =>
    post<Account>("/api/accounts", body),
  createTrade: (body: { account_id: number; setup_id?: number; symbol?: string; side?: string; risk_profile: string; overrides?: Record<string, number | boolean | null>; force?: boolean }) =>
    post<Trade>("/api/trades", body),
  tradeAction: (id: number, action: "open" | "cancel") => post<Trade>(`/api/trades/${id}/${action}`),
  closePreview: (id: number, fraction: number) => get<ClosePreview>(`/api/trades/${id}/close_preview?fraction=${fraction}`),
  closeTrade: (id: number, fraction: number) => post<Trade>(`/api/trades/${id}/close`, { fraction }),
  researchPosition: (id: number) => post<PositionResearch>(`/api/trades/${id}/research`),
  setNotes: (id: number, notes: string) => send<Trade>("PATCH", `/api/trades/${id}`, { notes }),
  renameAccount: (id: number, name: string) => send<Account>("PATCH", `/api/accounts/${id}`, { name }),
  deleteAccount: (id: number) => send<{ ok: boolean }>("DELETE", `/api/accounts/${id}`),
  ledgerUrl: (id: number) => `/api/accounts/${id}/ledger.csv`,
  prop: (accountId: number, range = "all") => get<PropPayload>(`/api/prop?account_id=${accountId}&range=${range}`),
};

async function post<T = unknown>(url: string, body?: unknown): Promise<T> {
  return send<T>("POST", url, body);
}

async function send<T = unknown>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, { method, headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401 && !url.endsWith("/api/login")) window.dispatchEvent(new Event(UNAUTHORIZED));
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail ?? msg; } catch { /* plain text */ }
    throw new Error(msg);
  }
  return r.json();
}
