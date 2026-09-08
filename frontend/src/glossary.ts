/**
 * Plain-English explanations for the terms Wick uses, plus what the current value implies.
 * Every entry answers two questions: what is this, and what does this number mean here?
 */
export type Gloss = { title: string; what: string; here?: string };

const pct = (v: unknown, d = 1) => (typeof v === "number" ? `${v >= 0 ? "+" : ""}${v.toFixed(d)}%` : "–");

export const GLOSSARY: Record<string, (v?: unknown, extra?: string) => Gloss> = {
  trend: (ok) => ({
    title: "Trend",
    what: "Whether the price is above or below its important moving averages (20, 50 and 200 hours). Above all of them is an uptrend, below all of them a downtrend, mixed means no clear direction.",
    here: ok === true ? "Here: price direction agrees with the averages, so there is a trend to join." : ok === false ? "Here: the averages disagree, so there is no clean trend. Wick will not take a side on trend alone." : undefined,
  }),
  volume: (ok, detail) => ({
    title: "Volume",
    what: "How much trading is happening compared with this coin's own 30-day average. A move on unusually high volume is more likely to be real than one on thin volume.",
    here: ok === true ? `Here: volume is elevated${detail ? ` (${detail})` : ""}, so the move has participation behind it.` : ok === false ? `Here: volume is ordinary${detail ? ` (${detail})` : ""}, so the move is not confirmed by activity.` : undefined,
  }),
  funding: (ok, detail) => ({
    title: "Funding",
    what: "A periodic payment between traders holding perpetual futures. Very positive funding means longs are crowded and paying to stay in; very negative means shorts are crowded. Crowded positioning raises the chance of a sharp squeeze the other way.",
    here: ok === true ? `Here: funding is near neutral${detail ? ` (${detail})` : ""}, so positioning is not heavily crowded either way.` : ok === false ? `Here: funding is stretched${detail ? ` (${detail})` : ""}, so one side is crowded and a squeeze is possible.` : undefined,
  }),
  rsi: (ok, detail) => ({
    title: "RSI",
    what: "Relative Strength Index, a 0 to 100 score of short-term momentum. Above 70 is stretched to the upside, below 30 stretched to the downside. Stretched readings often precede a pause or reversal, but can stay stretched in strong trends.",
    here: ok === true ? `Here: RSI is not at an extreme${detail ? ` (${detail})` : ""}, so momentum is not yet exhausted.` : ok === false ? `Here: RSI is at an extreme${detail ? ` (${detail})` : ""}, so entering now often means chasing.` : undefined,
  }),
  move: (ok, detail) => ({
    title: "Move",
    what: "Today's price change measured against how much this coin normally moves in a day (its ATR). 1 ATR is a typical day; 3 ATR is an unusually large one.",
    here: ok === true ? `Here: the move is meaningful${detail ? ` (${detail})` : ""}, big enough to matter relative to normal.` : ok === false ? `Here: the move is small${detail ? ` (${detail})` : ""}, within normal daily noise.` : undefined,
  }),
  flow: (ok, detail) => ({
    title: "Flow",
    what: "Which side is being aggressive. Taker buyers cross the spread to buy now; taker sellers do the opposite. Above 50% buying means buyers are pressing, below 50% sellers are.",
    here: ok === true ? `Here: aggressive flow agrees with the trend${detail ? ` (${detail})` : ""}.` : ok === false ? `Here: aggressive flow disagrees with the trend${detail ? ` (${detail})` : ""}, which weakens the case.` : "Here: no clear reading yet.",
  }),
  shape: (ok, detail) => ({
    title: "Shape",
    what: "The price pattern Wick detected over the last three days, from the geometry of the move: clean trend, breakout, squeeze, blow-off, dead-cat bounce and so on. Patterns describe what happened; they do not promise what happens next.",
    here: ok === true ? `Here: the pattern does not argue against the trade${detail ? ` (${detail})` : ""}.` : ok === false ? `Here: the pattern argues against it${detail ? ` (${detail})` : ""}.` : undefined,
  }),
  quantSignal: (v) => ({
    title: "Quant Signal",
    what: "Wick's rules-based view using current market data only: trend, volume, funding, RSI, move, flow and shape. It updates every scan for free and includes no web research. LONG means the rules favor a long setup, SHORT a short setup, FLAT means not enough evidence to take a side.",
    here: v === "long" ? "Here: the rules favor a long. Research it before acting." : v === "short" ? "Here: the rules favor a short. Research it before acting." : "Here: the rules see no side worth taking on the numbers alone.",
  }),
  aiResearch: () => ({
    title: "AI Research",
    what: "A one-off web and news check by the model, run only when you click Research. It is cached with a timestamp. CURRENT is under two hours old, AGING under six, STALE means time passed or the market moved materially since, so refresh before acting on it.",
  }),
  wickVerdict: () => ({
    title: "Wick Verdict",
    what: "The synthesis of the quant signal and the research: ENTER (build the trade), WAIT (thesis holds but not at this price), or PASS (do not trade this). If research is missing or stale, the verdict is to research first.",
  }),
  mainConcern: () => ({
    title: "Main concern",
    what: "The first rules check that failed. It is the single biggest reason Wick is not fully convinced, spelled out in plain English.",
  }),
  atr: (v) => ({
    title: "ATR",
    what: "Average True Range: how much this coin typically moves in a day, including gaps. Wick uses it as the yardstick for 'big' or 'small', and to place stops outside normal noise.",
    here: typeof v === "number" ? `Here: a typical day moves about ${v.toFixed(2)}%.` : undefined,
  }),
  r: () => ({
    title: "R",
    what: "Result measured in units of the risk you planned. +2R means you made twice what you would have lost at your stop; −1R means the stop was hit. It lets trades of different sizes be compared fairly.",
  }),
  rr: (v) => ({
    title: "R:R",
    what: "Reward-to-risk: distance to the target divided by distance to the stop. At 2.0 you stand to make twice what you risk. Wick advises against trades under 1.5.",
    here: typeof v === "number" ? `Here: ${v.toFixed(2)}, ${v >= 1.5 ? "acceptable" : "thin"}.` : "Here: no target, so no ratio.",
  }),
  stop: () => ({
    title: "Stop (invalidation)",
    what: "The price where the idea is proven wrong and the position is closed automatically. Wick places it beyond the recent swing plus a buffer, so ordinary noise does not trigger it.",
  }),
  target: () => ({
    title: "Target",
    what: "A price where taking profit is reasonable, drawn from structure such as the week's high or how far this pattern usually travels on this coin. Reaching it is flagged; closing is your decision.",
  }),
  exposure: () => ({
    title: "Position size (exposure)",
    what: "The market value you hold, not the cash you would have to put up. Gains and losses are calculated on this number.",
  }),
  riskUsd: () => ({
    title: "Loss if the stop fills",
    what: "The dollars you lose if price hits the stop. Wick sizes positions so this equals a chosen share of your equity (0.5 to 1 percent) instead of asking how much you want to spend.",
  }),
  openRisk: (v) => ({
    title: "Open risk",
    what: "The sum of what every open position would lose if all their stops filled at once, as a share of equity. Capped at 3% so a bad day cannot breach the account.",
    here: typeof v === "number" ? `Here: ${v.toFixed(2)}% of equity is at risk right now.` : undefined,
  }),
  drawdown: (v) => ({
    title: "Drawdown",
    what: "How far equity has fallen from its highest point. Prop challenges end the account when this exceeds a limit, usually 8 to 10 percent.",
    here: typeof v === "number" ? `Here: ${v.toFixed(2)}% below the peak.` : undefined,
  }),
  dailyLoss: () => ({
    title: "Daily loss limit",
    what: "The most an account may lose in one day before the challenge fails. Wick refuses trades whose stop-out would breach what is left of today's budget.",
  }),
  unusualness: (v) => ({
    title: "Unusualness",
    what: "How far from normal this coin's current state is: move in ATR units, volume multiple, funding crowding and cross-venue divergence, added together. Higher means more worth a look, not more worth buying.",
    here: typeof v === "number" ? `Here: ${v.toFixed(2)}.` : undefined,
  }),
  oi: () => ({ title: "Open interest", what: "The total value of perpetual-futures positions currently open. Rising open interest with a move means new money is joining it; falling means positions are closing." }),
  divergence: () => ({ title: "Kraken divergence", what: "Kraken's price minus Binance's, in basis points (1 bp = 0.01%). Usually tiny; a large gap can mean one venue is lagging or thin." }),
  takers: () => ({ title: "Taker buy share", what: "The share of the last 24 hours' volume that came from buyers crossing the spread. Above 50% buyers were pressing, below 50% sellers were." }),
  beta: () => ({ title: "BTC beta", what: "How much this coin moves when Bitcoin moves, over the last 60 days. 1.5 means it tends to move 1.5 times as much as Bitcoin." }),
  own: () => ({ title: "Own move", what: "Today's change after removing the part explained by Bitcoin. A big own move means the coin is moving for its own reasons, not just with the market." }),
  sigma: () => ({ title: "Vol per day", what: "A forecast of one day's typical swing, in percent, weighted toward recent days. Used to size positions so a normal day costs the same budget on every coin." }),
  slippage: () => ({ title: "Slippage", what: "How much worse than the mid price you would actually fill, from walking through the order book at your size. Thin books cost more." }),
  change24h: (v) => ({ title: "24h change", what: "Price change over the last 24 hours, a rolling window ending now.", here: `Here: ${pct(v)}.` }),
  quality: (v) => ({
    title: "Setup quality",
    what: "How many of Wick's rules checks pass. STRONG passes nearly all, MIXED passes about half, WEAK passes few. It says how clean the setup looks on the numbers, not whether to trade it.",
    here: typeof v === "string" ? `Here: ${v.toUpperCase()}.` : undefined,
  }),
};

/** Plain-English reason for the first failing check, using the check's own detail. */
export function explainConcern(name: string, detail?: string): string {
  const d = detail ? ` (${detail})` : "";
  if (name.startsWith("Trend")) return `Trend is weak — price is not consistently holding above or below its key moving averages, so there is no clear direction to join${d}.`;
  if (name.startsWith("Volume")) return `Volume is thin — trading activity is not unusual enough to confirm the move${d}.`;
  if (name.startsWith("Funding")) return `Funding is crowded — perpetual traders are already heavily positioned on one side, which raises squeeze risk${d}.`;
  if (name.startsWith("RSI")) return `RSI is at an extreme — short-term momentum is stretched, so entering here often means chasing${d}.`;
  if (name.startsWith("Move")) return `The move is small — price has not moved enough relative to its normal daily range to mean anything yet${d}.`;
  if (name.startsWith("Taker") || name.startsWith("Flow")) return `Flow disagrees — aggressive buyers and sellers are not pushing in the direction of the trend${d}.`;
  if (name.startsWith("Shape")) return `The price pattern argues against it — the recent shape is one that usually exhausts rather than continues${d}.`;
  return `${name}${d}`;
}

/** Friendlier one-liners for the shape labels. */
export const SHAPE_PLAIN: Record<string, string> = {
  blowoff_up: "An unusually vertical rise on extreme volume and momentum. Often the crowd is all in and the move exhausts, but it can run further than expected.",
  capitulation: "An unusually vertical drop on extreme volume with momentum pinned low. Forced selling; often followed by a bounce, sometimes by more selling.",
  flag_up: "A big rise, then a tight, quiet range. Textbooks expect continuation higher; check this coin's own base rate before believing it.",
  flag_down: "A big drop, then a tight, quiet range. Textbooks expect continuation lower.",
  dead_cat: "A hard fall followed by a partial bounce. Could be a real recovery or a pause before more selling; volume on the bounce decides.",
  v_reversal: "A hard fall that was mostly bought back quickly. Sellers were absorbed fast.",
  trend_up: "A clean, efficient rise with dips bought. A momentum regime.",
  trend_down: "A clean, efficient decline with rallies sold.",
  squeeze: "Volatility has compressed well below normal. A big move usually follows; the direction is not implied.",
  range: "Lots of motion, no net progress. The edges have held and breakouts have failed.",
  chop: "No recognisable pattern. Noise.",
};
