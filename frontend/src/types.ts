export type Interval = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";
export const INTERVALS: Interval[] = ["1m", "5m", "15m", "1h", "4h", "1d"];

// Times are SECONDS here. The backend converts from Binance milliseconds in exactly one
// place (timeutil.ms_to_s); the frontend never touches units.
export interface WireCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  closed: boolean;
}

export interface WireTicker {
  symbol: string;
  last: number;
  changePct: number;
  quoteVolume: number;
  high: number;
  low: number;
  time: number;
  exchange: string;
}

export interface MarketRow extends WireTicker {
  tracked: boolean;
  sparkline: number[] | null;
  others: Record<string, WireTicker>; // other exchanges' tickers for the same symbol
}

export interface FundingPoint {
  time: number;
  rate: number;
}

export interface FuturesData {
  symbol: string;
  source: string;
  markPrice: number | null;
  indexPrice: number | null;
  fundingRate: number | null;        // fraction per 8h
  nextFundingTime: number | null;    // seconds, null if the venue accrues continuously
  openInterest: number | null;
  time: number;
  basisPct: number | null;
  fundingAnnualizedPct: number | null;
  openInterestNotional: number | null;
  history: FundingPoint[];
  historySummary: { n: number; mean?: number; positiveShare?: number; min?: number; max?: number };
}

export interface CrossExchange extends WireTicker {
  divergenceBps: number | null;
  ageS: number;
}

export interface SymbolInfo {
  symbol: string;
  base: string;
  quote: string;
  tracked: boolean;
}

export interface WireDepth {
  symbol: string;
  bids: [number, number][];
  asks: [number, number][];
  time: number;
}

export interface Status {
  type: "status";
  /** false while stored history is still syncing after a start: research and trade actions wait. */
  ready?: boolean;
  ingest: {
    state: "connecting" | "live" | "reconnecting";
    lastFrameAgeS: number | null;
    historyReady?: boolean;
    syncProgress?: { done: number; total: number };
    reconnects: number;
    frames: number;
    rejectedStale: number;
    streams: number;
    tracked: string[];
    depthSymbols: string[];
  };
  rest: { usedWeight1m: number; calls: number; banned: boolean; banMessage: string };
  broadcast: { clients: number; droppedFrames: number };
  repairs: { symbol: string; interval: string; start: number; end: number; candles: number; source: string; at: number }[];
  exchanges: Record<string, { state: "connecting" | "live" | "reconnecting"; lastFrameAgeS: number | null; tracked: string[] }>;
  futures: { source: string | null; error: string | null };
}

export type ServerMsg =
  | Status
  | { type: "kline"; symbol: string; interval: Interval; candle: WireCandle }
  | { type: "ticker"; ticker: WireTicker }
  | { type: "depth"; depth: WireDepth }
  | { type: "backfilled"; symbol: string; interval: Interval };

export interface HorizonStats {
  horizonBars: number;
  n: number;
  underpowered: boolean;
  overlapping: boolean;
  medianGapBars: number | null;
  hitRateNet?: number;
  ciLo?: number;
  ciHi?: number;
  hitRateGross?: number;
  medianNet?: number;
  p25Net?: number;
  p75Net?: number;
  medianGross?: number;
  firstHalf?: { n: number; hitRateNet: number | null; ciLo: number | null; ciHi: number | null; underpowered: boolean };
  secondHalf?: { n: number; hitRateNet: number | null; ciLo: number | null; ciHi: number | null; underpowered: boolean };
}

export interface BaseRate {
  condition: string;
  nBars: number;
  nEpisodes: number;
  horizons: Record<string, HorizonStats>;
}

export interface ContextData {
  symbol: string;
  price: number;
  asOf: number;
  history: { bars1h: number; from: number; to: number; bars1d: number };
  volume: { last24h: number; avg30d: number | null; days: number; multiple: number | null };
  volatility: {
    realized30dAnnualized: number | null;
    realized7dAnnualized: number | null;
    atr14Daily: number | null;
    atr14DailyPct: number | null;
    todayMovePct: number | null;
    todayMoveInAtr: number | null;
    atr14Hourly: number | null;
  };
  ma: Record<string, { value: number | null; distancePct: number | null }>;
  rsi: {
    value: number | null;
    histogram: { bins: { lo: number; hi: number; count: number }[]; currentBin: number | null; percentile: number | null; n: number };
  };
  book: {
    mid: number;
    spreadBps: number;
    bands: { widthPct: number; bidNotional: number; askNotional: number; imbalance: number | null }[];
    coveragePct: number;
    levels: number;
    source: string;
    ageS: number;
  } | null;
  correlationBtc30d: number | null;
  flow: { takerBuyRatio24: number | null; takerBuyRatio7d: number | null; takerBuyRatio30dMean: number | null };
  impliedVol: { impliedVol30dPct: number; realizedVol30dPct: number | null; variancePremiumPct: number | null; source: string } | null;
  slippage: SlippageTable | null;
  accountSize: number;
  futures: FuturesData | null;
  futuresStatus: { source: string | null; error: string | null };
  crossExchange: Record<string, CrossExchange>;
  conditionsTrue: string[];
  baseRates: BaseRate[];
  testsEvaluated: { total: number; symbols: number; conditions: number; horizons: number };
  minSample: number;
  roundTripCost: number;
  caveats: string;
}

export interface Features {
  symbol: string;
  price: number;
  change24hPct: number | null;
  moveAtr: number | null;
  atrDailyPct: number | null;
  volMultiple: number | null;
  rsi: number | null;
  ma20Pct: number | null;
  ma50Pct: number | null;
  ma200Pct: number | null;
  fundingRate: number | null;
  openInterestNotional: number | null;
  divergenceBps: number | null;
  takerBuyRatio24: number | null;
  takerBuyRatio30d: number | null;
  betaBtc: number | null;
  residual24hPct: number | null;
  ewmaDailyVolPct: number | null;
}

export interface SlippageTable {
  mid: number;
  levels: number;
  rows: { notional: number; buyBps: number | null; sellBps: number | null; buyFilled: number; sellFilled: number }[];
}

export interface RulesVerdict {
  source: "rules";
  stance: "long" | "short" | "flat";
  trend: string;
  horizonH: number;
  invalidation: number | null;
  invalidationPct: number | null;
  suggestedNotionalPct: number | null;
  volTargetNotionalPct: number | null;
  checks: { name: string; ok: boolean | null; detail: string }[];
  summary: string;
}

export interface ModelAnalysis {
  symbol: string;
  time: number; // ms
  model: string;
  stance: "long" | "short" | "flat";
  confidence: string;
  horizonH: number;
  invalidation: number;
  trend: string;
  summary: string;
  price: number;
  drivers: { text: string; url: string }[];
  risks: string[];
  numbers: string[];
  citations: { url: string; title: string }[];
  usage: { total_tokens?: number };
}

export interface ShapeReport {
  label: string;
  name: string;
  description: string;
  metrics: Record<string, number | boolean | null>;
  nEpisodes: number;
  nBars: number;
  horizons: Record<string, HorizonStats>;
  history: { label: string; count: number }[];
}

export interface SetupResearch {
  time: number;
  summary: string;
  thesisVerdict: "strengthened" | "unchanged" | "weakened";
  mainRisk: string;
  confidence: string;
  trend: string;
  horizonH: number;
  invalidation: number;
  drivers: { text: string; url: string }[];
  risks: string[];
  model: string;
}

export interface Setup {
  id: number;
  state: "scanned" | "researched" | "passed" | "expired";
  quality: "strong" | "mixed" | "weak";
  bias: "long" | "short" | "neutral";
  concern: string;
  recommendation: "enter" | "wait" | "pass" | null;
  detectedAt: number;
  timeframe: string;
  horizonH: number;
  checks: { name: string; ok: boolean | null; detail: string }[];
  research: SetupResearch | null;
  researchState: "none" | "fresh" | "aging" | "stale";
  researchAgeS: number | null;
  staleReasons: string[];
  researchTrigger: "manual" | "auto" | null;
  liveSignal: "long" | "short" | "flat";
  pinned: boolean;
}

export interface LlmUsage {
  calls: number;
  estCostUsd: number;
  autoResearch: boolean;
  recent: { symbol: string; time: number; trigger: string; inputTokens: number; outputTokens: number; estCostUsd: number }[];
}

export interface Held {
  tradeId: number;
  accountId: number;
  state: string;
  side: string;
  status: string;
  unrealizedUsd: number | null;
  unrealizedR: number | null;
}

export interface Mover {
  symbol: string;
  score: number;
  features: Features;
  rules: RulesVerdict | null;
  model: ModelAnalysis | null;
  shape: ShapeReport | null;
  slippage: SlippageTable | null;
  setup: Setup | null;
  held: Held[];
}

export interface Briefing {
  newSetups: number;
  researched: number;
  waiting: number;
  ready: number;
  open: number;
  attention: number;
  openPnlUsd: number;
}

export interface Plan {
  side: "long" | "short";
  action: "enter" | "wait";
  entry: number;
  trigger_kind: "pullback" | "breakout" | null;
  invalidation: number;
  target: number | null;
  target_source: string | null;
  rr: number | null;
  recommendation: "enter" | "wait" | "pass";
  reason: string;
  planner_version: string;
}

export interface Sizing {
  notionalUsd: number;
  riskUsd: number;
  riskPct: number;
  riskFrac: number;          // loss fraction of notional if the stop fills
  maxNotionalUsd: number;    // the account's true ceiling for this stop
  maxRiskUsd: number;
  openRiskAfterPct: number;
  openRiskCapPct: number;
  blocked: boolean;
  blocks: string[];
}

export interface Account {
  id: number;
  name: string;
  size: number;
  daily_loss_pct: number;
  max_dd_pct: number;
  target_pct: number;
  equity: number;
  realized: number;
  unrealized: number;
  returnPct: number;
  targetUsd: number;
  targetProgressPct: number;
  openRiskUsd: number;
  openRiskPct: number;
  todayPnlUsd: number;
  dailyLossRemainingUsd: number;
  drawdownPct: number;
  breached: boolean;
  stats: { n: number; winRate: number | null; expectancyR: number | null; maxDrawdownPct: number; curve: { time: number; equity: number }[]; byShape: Record<string, { n: number; sumR: number; wins: number }> };
  counts: { open: number; waiting: number; ready: number; attention: number };
}

export interface PlanResponse {
  setup: { id: number; symbol: string; state: string; bias: string; recommendation: string | null } | null;
  symbol: string;
  plan: Plan;
  sizing: Sizing;
  account: Account;
  riskProfiles: Record<string, number>;
  advisory: boolean;
  researched: boolean;
  manual: boolean;
}

export interface Trade {
  id: number;
  account_id: number;
  setup_id: number;
  symbol: string;
  side: "long" | "short";
  state: "waiting" | "ready" | "open" | "closed" | "cancelled";
  planner_version: string;
  risk_profile: string;
  plan_entry: number;
  trigger_kind: string | null;
  stop: number;
  target: number | null;
  size_usd: number;
  risk_usd: number;
  horizon_h: number;
  created_at: number;
  expires_at: number | null;
  opened_at: number | null;
  entry: number | null;
  closed_at: number | null;
  exit: number | null;
  exit_reason: string | null;
  pnl_usd: number | null;
  r_multiple: number | null;
  status: string;
  status_note: string;
  payload: { plan: Plan; thesis?: string; shapeAtEntry?: string; againstAdvice?: boolean; wickSaid?: string;
    costs?: { gross: number; fees: number; funding: number; hoursHeld: number; net: number };
    partials?: { time: number; fraction: number; price: number; pnl: number }[];
    positionResearch?: PositionResearch };
  notes?: string | null;
  unrealizedUsd?: number;
  unrealizedR?: number | null;
  currentPrice?: number;
  changes?: string;
}

export interface PositionResearch {
  time: number;
  action: "hold" | "watch_closely" | "reduce" | "consider_exit";
  reason: string;
  whatChanged: string;
  summary: string;
  risks: string[];
  drivers: { text: string; url: string }[];
  confidence: string;
  price: number;
  model: string;
}

export interface ClosePreview {
  price: number;
  fraction: number;
  closeUsd: number;
  realizedUsd: number;
  realizedR: number | null;
  remainingUsd: number;
  remainingRiskUsd: number;
  costs: { gross: number; fees: number; funding: number; hoursHeld: number; net: number };
}

export interface PropPayload {
  account: Account;
  open: Trade[];
  waiting: Trade[];
  closed: Trade[];
  judges: Record<string, { n: number; sumR: number; wins: number }>;
  equity: { time: number; equity: number }[];   // minute snapshots, open P&L included
}

export interface Tracker {
  todayPnlUsd: number;
  todayPnlPct: number;
  dailyLimitPct: number;
  dailyBudgetUsedPct: number | null;
  drawdownPct: number;
  maxDrawdownLimitPct: number;
  drawdownUsedPct: number | null;
  breached: boolean;
}

export interface AnalysisPayload {
  desk: Briefing | null;
  lastScan: number;
  scanIntervalS: number;
  llm: { configured: boolean; model: string; error: string | null; verified: boolean; callsToday: number; dailyCap: number | null; topMovers: number; ttlH: number; usage: LlmUsage };
  movers: Mover[];
  breadth: { n: number; above20?: number; above50?: number; above200?: number; up24h?: number };
  tracker: Tracker;
  dvol: Record<string, number>;
  alerts: { configured: boolean; discord?: boolean; telegram?: boolean; errors?: number; recent: { time: number; kind: string; text: string }[] };
  dailyLossBudgetPct: number;
  accountSize: number;
}

export interface Recommendation {
  id: number;
  symbol: string;
  source: string;
  time: number;
  stance: string;
  horizon_h: number;
  entry: number;
  invalidation: number | null;
  funding_rate: number | null;
  ret_1h: number | null;
  ret_4h: number | null;
  ret_24h: number | null;
  ret_72h: number | null;
  closed_at: number | null;
  exit: number | null;
  exit_reason: string | null;
  pnl_pct: number | null;
}

export interface Scoreboard {
  n: number;
  equity: number;
  returnPct?: number;
  hitRate?: number;
  maxDrawdownPct?: number;
  worstDayPct?: number;
  bestDayShare?: number | null;
  curve?: { time: number; equity: number }[];
}

export interface PaperPayload {
  open: Recommendation[];
  closed: Recommendation[];
  scoreboard: { rules: Scoreboard; model: Scoreboard };
  notional: number;
  startEquity: number;
}

export interface ConditionEvent {
  symbol: string;
  condition: string;
  time: number;
  price: number;
  ret1h: number | null;
  ret4h: number | null;
  ret24h: number | null;
}
