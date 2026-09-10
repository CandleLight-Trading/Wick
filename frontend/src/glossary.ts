/**
 * Plain-English explanations for the terms Wick uses, built the way a concept is actually
 * learned: intuition first, then what the number on screen means here, then the formal term.
 *
 *   title   the idea in plain words ("Typical movement")
 *   term    the vocabulary being taught ("ATR · Average True Range")
 *   here    what the current value implies, when a value is known (deterministic templates)
 *   what    the intuition, one or two sentences, and why it matters to the decision
 *   formal  the definition, last, so the jargon lands after the idea
 *
 * Observation is kept apart from prediction: nothing here says what price "will" do.
 */
export type Gloss = { title: string; term?: string; here?: string; what: string; formal?: string };
export type Ctx = { symbol?: string; atrPct?: number | null; price?: number | null; equity?: number | null; riskUsd?: number | null };

const num = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const pct = (v: unknown, d = 1) => (num(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(d)}%` : "–");
const sym = (c?: Ctx) => c?.symbol?.replace("USDT", "") ?? "This coin";

export const GLOSSARY: Record<string, (v?: unknown, extra?: string, ctx?: Ctx) => Gloss> = {
  // ---- the seven checks on a setup card (value = pass/fail, extra = the check's detail) ----
  trend: (ok, detail) => ({
    title: "Direction",
    term: "Trend · price vs 20 / 50 / 200-hour moving averages",
    here: ok === true ? `Price is on the same side of all three averages${detail ? ` (${detail})` : ""}: it has been moving one way rather than oscillating, so there is a direction to join.`
        : ok === false ? `The averages disagree${detail ? ` (${detail})` : ""}: price has been oscillating rather than going somewhere. Wick will not take a side on direction alone.` : undefined,
    what: "Has this coin been consistently going somewhere, or wobbling around the same level? A move in the direction of an existing trend has the wind behind it; a move against it is fighting the tape.",
    formal: "Trend here means price is above (uptrend) or below (downtrend) its 20, 50 and 200-hour moving averages. Mixed means no clear direction.",
  }),
  volume: (ok, detail) => ({
    title: "How unusual is today's activity?",
    term: "Relative volume · last 24h vs 30-day average",
    here: ok === true ? `Trading activity is elevated${detail ? ` (${detail} its normal level)` : ""}: this move is attracting substantially more participation than usual.`
        : ok === false ? `Activity is ordinary${detail ? ` (${detail} its normal level)` : ""}: the move has not drawn a crowd, which makes it easier to fade.` : undefined,
    what: "A busy day means many participants are acting on the same thing, so the move is more likely to be real than a quiet-day drift. High volume strengthens the significance of a move; direction still depends on price and flow.",
    formal: "Relative volume compares the last 24 hours of volume with the coin's own average over the last 30 days. Wick asks for at least 1.3× to confirm a move.",
  }),
  funding: (ok, detail) => ({
    title: "Which side is crowded?",
    term: "Funding rate · perpetual futures, per 8 hours",
    here: ok === true ? `Funding is near neutral${detail ? ` (${detail})` : ""}: neither side is paying much to hold, so positioning is not stretched.`
        : ok === false ? `Funding is stretched${detail ? ` (${detail})` : ""}: one side is paying up to stay in. A crowded side is fuel for a sharp move the other way.` : undefined,
    what: "Perpetual futures never expire, so every eight hours one side pays the other to keep the contract near spot. Persistent positive funding means longs are crowded and paying; negative means shorts are. Modest funding is normal. Extreme funding is a warning about who gets squeezed.",
    formal: "The funding rate is the periodic payment between long and short perpetual-futures holders, quoted per 8 hours. Wick flags it as crowded beyond a threshold in either direction.",
  }),
  rsi: (ok, detail) => ({
    title: "Recent momentum",
    term: "RSI · Relative Strength Index, hourly, 0–100",
    here: ok === true ? `Momentum is not at an extreme${detail ? ` (${detail})` : ""}: recent moves have not been one-sided enough to suggest exhaustion.`
        : ok === false ? `Momentum is stretched${detail ? ` (${detail})` : ""}: price has moved unusually consistently one way. That is strong momentum, not a reversal signal by itself, but entering here often means chasing.` : undefined,
    what: "How one-sided recent price action has been. Above 70 the last stretch was mostly up; below 30 mostly down. Strong trends stay stretched for a long time, so Wick uses it as a filter against chasing, never as a trigger to fade.",
    formal: "RSI compares the size of recent up-moves with recent down-moves over a 14-bar window, scaled 0 to 100.",
  }),
  move: (ok, detail, ctx) => ({
    title: "How big is this move for this coin?",
    term: "Move in ATR · today's change ÷ typical daily range",
    here: num(ok) ? interpretMove(ok, ctx)
        : ok === true ? `The move is meaningful${detail ? ` (${detail})` : ""}: larger than this coin's ordinary daily noise.`
        : ok === false ? `The move is small${detail ? ` (${detail})` : ""}: within what this coin does on an ordinary day, so it may mean nothing yet.` : undefined,
    what: "A 5% day is dramatic for Bitcoin and unremarkable for a small coin. Measuring the move against the coin's own typical daily range says whether anything unusual is happening. Around 1 ATR is a normal day; 3 ATR is an event, and also a warning about entering late.",
    formal: "The change since yesterday's close divided by the 14-day Average True Range. Wick asks for at least 0.5 ATR to count a move as meaningful, and treats more than 1 ATR as extended.",
  }),
  flow: (ok, detail) => ({
    title: "Who is being more aggressive?",
    term: "Taker flow · share of volume that was aggressive buying",
    here: ok === true ? `Aggressors agree with the trend${detail ? ` (${detail})` : ""}: the side in a hurry is the side the trade is on.`
        : ok === false ? `Aggressors disagree with the trend${detail ? ` (${detail})` : ""}: the people crossing the spread are on the other side, which weakens the case.` : "No clear reading yet.",
    what: "Every trade has a patient side that placed a resting order and an urgent side that crossed the spread to hit it. The urgent side tells you who is in a hurry. Above 50% buyers were pressing; below, sellers were. This is not 'x% of traders are bullish'; it is who paid to act now.",
    formal: "Taker flow is taker-buy volume divided by total volume over the last 24 hours, compared with the coin's own 30-day baseline.",
  }),
  shape: (ok, detail) => ({
    title: "What does the last three days look like?",
    term: "Shape · regression slope, efficiency ratio, variance ratio",
    here: ok === true ? `The pattern does not argue against the trade${detail ? ` (${detail})` : ""}.`
        : ok === false ? `The pattern argues against it${detail ? ` (${detail})` : ""}: this geometry has more often exhausted than continued.` : undefined,
    what: "The geometry of the recent move, given a name: clean trend, flag, squeeze, blow-off, capitulation, chop. Wick also checks how the same shape resolved on this coin before. A shape describes what happened; it does not promise what happens next.",
    formal: "Labels come from the slope and fit of a rolling regression, the efficiency ratio (net progress ÷ total travel), the variance ratio, bandwidth compression, and volume climax over the last 72 hourly bars.",
  }),

  // ---- the three boxes on a setup card ----
  quantSignal: (v) => ({
    title: "What the numbers say on their own",
    term: "Quant Signal · deterministic playbook rules, no AI",
    here: v === "long" ? "The current data mechanically favors a long setup. This is an evidence classification from fixed rules, not a prediction and not a promise."
        : v === "short" ? "The current data mechanically favors a short setup. An evidence classification from fixed rules, not a prediction."
        : "No playbook's checks all pass right now. The numbers alone do not justify a side.",
    what: "Wick checks every coin against six playbooks: trend continuation, pullback, relative strength, momentum breakout, blow-off reversal, crowded squeeze. The first whose checks all pass gives the signal. Every check is shown, so a near miss is visible.",
    formal: "Recomputed every scan from stored exchange candles. LONG, SHORT or FLAT, with the matched playbook and its risk character (standard, moderate, aggressive).",
  }),
  aiResearch: () => ({
    title: "What is happening outside the chart",
    term: "AI Research · bounded web search, on request only",
    what: "One model call, at most three web searches, that classifies fresh external information for the setup as SUPPORTIVE, NEUTRAL or ADVERSE. NEUTRAL means nothing material was found, and that does not by itself invalidate a strong quantitative setup. It runs only when you press Research.",
    formal: "Research ages: CURRENT under two hours, AGING under six, STALE when time has passed or the market moved materially since (price beyond a daily ATR, shape changed, signal flipped). Stale is labelled, never refreshed on its own.",
  }),
  wickVerdict: () => ({
    title: "Wick's advice, in one word",
    term: "Wick Verdict · ENTER, WAIT or PASS",
    what: "The quant signal, modified by research. ENTER: build the trade. WAIT: the direction is attractive but the price is not, usually because the move is already extended; Wick watches for the better entry. PASS: the reward is too small for the planned loss, or research found something material. It is advice: Take It Anyway is always available.",
    formal: "Veto → PASS. Weaken → WAIT. Otherwise the playbook's own entry action. With no matching playbook: WAIT if research is supportive, else PASS.",
  }),
  mainConcern: () => ({
    title: "The biggest reason Wick is not convinced",
    term: "Main concern · first failing check",
    what: "The first playbook check that failed, spelled out. It is the one thing that would most change the picture if it flipped.",
  }),
  quality: (v) => ({
    title: "How clean does the setup look?",
    term: "Setup quality · share of checks passing",
    here: typeof v === "string" ? `${v.toUpperCase()}: ${v === "strong" ? "nearly every check passes." : v === "mixed" ? "about half the checks pass." : "few checks pass."}` : undefined,
    what: "How many of the playbook's checks pass. It says how tidy the evidence is on the numbers, not whether to trade it.",
  }),

  // ---- the metric strip in Quant Details (value = the number) ----
  atr: (v, _e, ctx) => ({
    title: "Typical movement",
    term: "ATR · Average True Range, 14 days",
    here: num(v) ? `${sym(ctx)} has recently moved about ${v.toFixed(1)}% per day under normal conditions. A ${(v / 4).toFixed(1)}% move is fairly ordinary; a ${(v * 3).toFixed(0)}% move would be unusually large.` : undefined,
    what: "How much this coin normally thrashes around in a day. Wick measures every move, stop and target in this unit, so 'big' and 'small' mean the same thing on every coin, and stops sit outside ordinary noise.",
    formal: "ATR averages the full daily range, including any gap from the prior close, over 14 days.",
  }),
  unusualness: (v) => ({
    title: "How far from normal is this coin right now?",
    term: "Unusualness score",
    here: num(v) ? `${v.toFixed(2)}: ${v < 1 ? "close to an ordinary day." : v < 3 ? "noticeably out of the ordinary." : "far outside its normal behavior."} Higher means more worth a look, not more worth buying.` : undefined,
    what: "Move in ATR, the volume multiple, funding crowding and cross-venue divergence, added together. It ranks what deserves attention; it says nothing about direction.",
  }),
  oi: (v) => ({
    title: "How much leveraged money is in this coin?",
    term: "Open interest · notional value of open perpetual positions",
    here: num(v) ? `About $${compact(v)} of perpetual positions are open. Rising open interest during a move means new money is joining it; falling means positions are closing.` : undefined,
    what: "The total size of futures bets currently open. On its own it has no direction; its change during a move tells you whether the move is being built or unwound.",
  }),
  divergence: (v) => ({
    title: "Do the venues agree on the price?",
    term: "Cross-exchange divergence · Kraken minus Binance, basis points",
    here: num(v) ? `${v.toFixed(1)} bps (${(v / 100).toFixed(2)}%). ${Math.abs(v) < 10 ? "Ordinary; the venues agree." : "Unusually wide; one venue may be lagging or thin."}` : undefined,
    what: "Usually the same coin trades at almost the same price everywhere. A large gap means one venue is lagging, thin, or seeing flow the other is not.",
    formal: "1 basis point = 0.01%.",
  }),
  takers: (v) => ({
    title: "Who is being more aggressive?",
    term: "Taker buy share · last 24 hours",
    here: num(v) ? `${(v * 100).toFixed(0)}% of taker volume came from buyers: ${v > 0.55 ? "buyers are crossing the spread more urgently than sellers." : v < 0.45 ? "sellers are crossing the spread more urgently than buyers." : "neither side is clearly more urgent."}` : undefined,
    what: "Market orders consume resting liquidity; whoever sends them is in a hurry. This says who is urgent, not how many people are bullish.",
  }),
  beta: (v, _e, ctx) => ({
    title: "How hard does it react to Bitcoin?",
    term: "Beta to BTC · 60-day daily returns",
    here: num(v) ? `Historically, when Bitcoin moves 1%, ${sym(ctx)} has tended to move about ${v.toFixed(1)}% in the same direction over the measured window.` : undefined,
    what: "Most coins move with Bitcoin to some degree. Beta says how much. It is a tendency over a window, not a law, and it says nothing about the coin's own news.",
  }),
  own: (v, _e, ctx) => ({
    title: "How much of the move is the coin's own?",
    term: "Residual move · 24h change minus beta × BTC's change",
    here: num(v) ? `${pct(v)} after removing what Bitcoin's move would explain. ${Math.abs(v) > 3 ? `${sym(ctx)} is moving for its own reasons, not just with the market.` : "Most of today's move is the market, not this coin."}` : undefined,
    what: "A coin up 4% on a day Bitcoin is up 4% has done nothing unusual. Subtracting the market's share leaves the part that needs its own explanation.",
  }),
  sigma: (v, _e, ctx) => ({
    title: "How big is a normal day, right now?",
    term: "Daily volatility forecast · exponentially weighted",
    here: num(v) ? `A normal day for ${sym(ctx)} is currently about ±${v.toFixed(1)}%, weighted toward recent days.` : undefined,
    what: "Like ATR, but leaning on recent days, so it responds faster when a coin gets wilder or calmer. Wick can size positions so that a normal day costs the same budget on every coin.",
  }),
  slippage: (v) => ({
    title: "What will it actually cost to get in?",
    term: "Slippage · fill price vs displayed price, from the order book",
    here: num(v) ? `A market order at your size is estimated to fill about ${(v / 100).toFixed(2)}% worse than the displayed price.` : undefined,
    what: "The displayed price is the best resting order. A larger order walks through several levels, each a little worse. Thin books cost more. Wick adds the estimate to the entry so the plan is honest.",
  }),
  change24h: (v) => ({ title: "Change over the last day", term: "24h change · rolling window ending now", here: `${pct(v)} over the last 24 hours.`, what: "A rolling window, not the calendar day, so it moves continuously." }),

  // ---- the ticket and the account (value = the number) ----
  r: (v, _e, ctx) => ({
    title: "One unit of the risk you agreed to take",
    term: "R · planned loss at the stop",
    here: num(ctx?.riskUsd) ? `You are risking $${ctx!.riskUsd!.toFixed(0)} if the stop fills. That $${ctx!.riskUsd!.toFixed(0)} is 1R. A $${(ctx!.riskUsd! * 2).toFixed(0)} profit would be +2R; the stop is −1R.` : num(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}R: ${Math.abs(v).toFixed(2)} times the loss you had planned at the stop.` : undefined,
    what: "Instead of comparing arbitrary dollar amounts, results are counted in units of the loss you planned. A +2R trade on a small position and on a large one are the same quality of decision.",
  }),
  rr: (v) => ({
    title: "What the target pays for what the stop can lose",
    term: "Reward / Risk · target distance ÷ stop distance",
    here: num(v) ? `This plan targets $${v.toFixed(2)} of potential profit for every $1.00 planned at risk. ${v >= 1.5 ? "Acceptable." : "Thin: Wick advises against plans under 1.5×, though you may take it."} It does not say the trade is likely to win; it describes the payoff if the stop and target are reached as planned.` : "No target, so no ratio: you would manage the exit yourself.",
    what: "A plan can be right often and still lose money if the wins are small and the losses are full size. The ratio keeps the payoff honest before the trade exists.",
  }),
  stop: (v) => ({
    title: "The price at which the idea is wrong",
    term: "Stop · invalidation",
    here: num(v) ? `The stop sits ${v.toFixed(2)}% from entry. Once the position is open, Wick fills it automatically, like a real account: the stop is an order, not an intention.` : undefined,
    what: "Not the price at which you feel uncomfortable; the price at which the reason for the trade no longer exists. Wick places it beyond the recent swing plus a buffer, so ordinary noise does not trigger it, and sets it before size, never after.",
  }),
  target: () => ({
    title: "Where taking profit is reasonable",
    term: "Target · structural level",
    what: "Drawn from structure: the week's high, a prior swing, or how far this shape usually travels on this coin. Reaching it is flagged; closing is your decision.",
  }),
  exposure: (v, _e, ctx) => ({
    title: "How much market you are holding",
    term: "Position size · notional exposure",
    here: num(v) && num(ctx?.equity) ? `$${Math.round(v).toLocaleString()}, about ${((v / ctx!.equity!) * 100).toFixed(0)}% of the account's equity, is exposed to this coin's moves. Gains and losses are calculated on this number.` : undefined,
    what: "Wick works backward: it takes the loss you are willing to bear at the stop and divides by the stop distance. A far stop means a smaller position; a volatile coin sizes itself down automatically.",
  }),
  riskUsd: (v, _e, ctx) => ({
    title: "Planned loss",
    term: "Position risk · 1R",
    here: num(v) ? `If the stop is reached as planned, this trade loses about $${v.toFixed(0)}${num(ctx?.equity) ? `, ${((v / ctx!.equity!) * 100).toFixed(2)}% of the account` : ""}. That is 1R for this trade.` : undefined,
    what: "The one number chosen deliberately. Wick sizes the position so a stop-out costs a fixed slice of equity (0.5% to 1% by profile, at most 0.5% for aggressive playbooks) instead of asking how much you want to spend.",
  }),
  openRisk: (v) => ({
    title: "What every open stop would cost at once",
    term: "Open risk · sum of planned losses, % of equity",
    here: num(v) ? `Your open positions could lose about ${v.toFixed(2)}% of equity if every planned stop were reached. The cap is 3%.` : undefined,
    what: "Not the current unrealized P&L; the combined planned loss across positions. It is what a bad day costs, and the cap is one of only two rules in Wick that block a trade outright.",
  }),
  drawdown: (v) => ({
    title: "How far below your high-water mark",
    term: "Drawdown · decline from peak equity",
    here: num(v) ? `The account is ${v.toFixed(2)}% below its highest recorded equity.` : undefined,
    what: "Measured from the peak, including open positions, not from the starting balance. A winning streak raises the bar. Prop challenges end the account when this exceeds a limit, usually 8 to 10 percent.",
  }),
  dailyLoss: () => ({
    title: "How much today is still allowed to lose",
    term: "Daily loss limit · remaining budget",
    what: "Lose this much in one day and trading stops until tomorrow. Wick refuses a trade whose stop-out would breach what is left of today's budget: the other rule that blocks outright.",
  }),

  // ---- Context tab ----
  ctxMoveToday: (v, _e, ctx) => ({
    title: "Which way, and how far, since the day began",
    term: "Move since yesterday's UTC close",
    here: num(v) ? `${sym(ctx)} is ${v >= 0 ? "up" : "down"} ${Math.abs(v).toFixed(2)}% since the previous UTC daily close. That is direction and size, not whether the move is unusual for ${sym(ctx)}: the next row answers that.` : undefined,
    what: "Raw percentages mislead across coins. A 2% move is a quiet day for a small coin and a notable one for Bitcoin, so Wick always pairs this number with the move in ATR.",
  }),
  ctxAtrDaily: (v, _e, ctx) => ({
    title: "Typical movement in a day",
    term: "ATR(14) on daily bars",
    here: num(v) ? `${sym(ctx)}'s daily range has recently averaged about ${v.toFixed(2)}% of price${num(ctx?.price) ? ` (about $${(ctx!.price! * v / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })})` : ""}. A move materially smaller than this may simply be ordinary daily movement.` : undefined,
    what: "How much this coin normally thrashes around in a day. It measures movement, not direction, and it is the yardstick Wick uses for every stop, target and move.",
    formal: "Average True Range: the average of the full daily range, including any gap from the prior close, over 14 days.",
  }),
  ctxAtrHourly: (v, _e, ctx) => ({
    title: "Typical movement in an hour",
    term: "ATR(14) on hourly bars",
    here: num(v) ? `${sym(ctx)} has recently moved about $${v.toLocaleString(undefined, { maximumFractionDigits: 2 })} during a typical hourly bar${num(ctx?.price) ? ` (${(v / ctx!.price! * 100).toFixed(2)}%)` : ""}. Wick uses it to judge whether short-term moves, stops and targets are large or small relative to normal hourly noise.` : undefined,
    what: "The same idea as the daily ATR, one bar at a time. A stop closer than an hour or two of this is inside the noise.",
  }),
  ctxRealizedVol: (v, win, ctx) => ({
    title: "How much it has actually been moving",
    term: `Realized volatility, ${win ?? ""}, annualized`,
    here: num(v) ? `${sym(ctx)}'s actual price movement over the last ${win ?? "window"} corresponds to about ${v.toFixed(0)}% annualized volatility. Compare the 7-day and 30-day figures: the shorter one says whether things are calmer or wilder than the recent norm.` : undefined,
    what: "Volatility that has already happened, from the spread of recent returns. Annualized so different windows can be compared; it is not a prediction that the coin moves this much in a year.",
    formal: "Standard deviation of hourly log returns over the window, scaled by the square root of the number of hours in a year.",
  }),
  ctxImpliedVol: (v, _e, ctx) => ({
    title: "How much movement the options market is paying for",
    term: "Implied volatility · DVOL, 30 days, annualized",
    here: num(v) ? `Options on ${sym(ctx)} are currently priced as if it will move about ${v.toFixed(1)}% annualized over the next month. Compare with realized volatility above: implied is what traders expect and pay for, realized is what happened.` : undefined,
    what: "Options cost more when traders expect bigger moves. Working backward from their prices gives the movement the market is bracing for. It is a consensus expectation, not a forecast that comes true.",
    formal: "Deribit's DVOL index, a 30-day forward-looking implied volatility for BTC and ETH.",
  }),
  ctxVariancePremium: (v) => ({
    title: "Are options bracing for more than has happened?",
    term: "Variance premium · implied minus realized, points",
    here: num(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(1)} points: ${Math.abs(v) < 3 ? "implied volatility is close to what has recently happened; the options market is not bracing for much more or less than the recent norm." : v > 0 ? "options are pricing more movement than has recently occurred, which often means traders are paying for protection." : "options are pricing less movement than has recently occurred; recent turbulence is expected to fade."} Positive or negative is not bullish or bearish on its own.` : undefined,
    what: "Implied minus realized volatility. A gap says whether the market expects the future to be wilder or calmer than the recent past.",
  }),
  ctxTakerBaseline: (v, base) => ({
    title: "Is today's urgency normal for this coin?",
    term: "Taker buy share · 7-day and 30-day means",
    here: num(v) && base && Number.isFinite(Number(base)) ? `Today's ${(v * 100).toFixed(0)}% is ${v > Number(base) + 0.02 ? "above" : v < Number(base) - 0.02 ? "below" : "about level with"} this coin's recent ${(Number(base) * 100).toFixed(0)}% baseline, so buying pressure is ${v > Number(base) + 0.02 ? "stronger" : v < Number(base) - 0.02 ? "weaker" : "about as strong"} as usual.` : undefined,
    what: "Some coins run structurally above or below 50% aggressive buying. Only the comparison with the coin's own baseline says whether today is different.",
  }),
  ctxMa: (v, n) => ({
    title: n === "20" ? "Short-term reference" : n === "50" ? "Medium-term reference" : "Longer-term reference",
    term: `${n}-hour moving average · distance of price from it`,
    here: num(v) ? `Price is ${Math.abs(v).toFixed(2)}% ${v >= 0 ? "above" : "below"} the ${n}-hour average. ${n === "200" ? "Below the 200 does not automatically mean sell, nor above mean buy; it says which regime the last week or so has been in." : "Distance, not a crossover signal."}` : undefined,
    what: n === "20" ? "The average price of the last 20 hours: where price has recently been. Pullbacks in a trend often return to it." : n === "50" ? "The average of the last two days or so. A medium-term reference for the direction of the move." : "The average of the last eight days or so. A slower reference often used to separate broader regimes.",
  }),
  ctxCorrelation: (v, _e, ctx) => ({
    title: "Does it move with Bitcoin?",
    term: "Correlation to BTC · 30 days of daily returns, −1 to +1",
    here: num(v) ? `${v.toFixed(2)}: ${sym(ctx)}'s daily returns have ${v > 0.7 ? "moved closely with Bitcoin, so a Bitcoin move may explain much of what you see here" : v > 0.3 ? "moved with Bitcoin some of the time" : v > -0.3 ? "moved largely independently of Bitcoin recently" : "moved against Bitcoin recently"}. If everything you watch sits near 0.9 you effectively hold one position.` : undefined,
    what: "How consistently two things move together, not whether one causes the other. Bitcoin itself is not shown because its correlation to itself is trivially 1.",
  }),
  ctxRsi: (v, pctl, ctx) => ({
    title: "Recent momentum, and how it compares with this coin's own history",
    term: "RSI(14), hourly · with percentile of this coin's readings",
    here: num(v) ? `RSI is ${v.toFixed(1)}: ${v > 70 ? "recent price action has been unusually one-sided upward. Strong momentum, not a reversal signal by itself." : v < 30 ? "recent price action has been unusually one-sided downward. Weak momentum, not a buy signal by itself." : v > 55 ? "slightly stronger recent momentum, nothing close to an extreme." : v < 45 ? "slightly weaker recent momentum, nothing close to an extreme." : "balanced recent momentum."}${pctl ? ` The ${pctl}th percentile means roughly ${100 - Number(pctl)}% of ${sym(ctx)}'s recorded hourly RSI readings were higher than the current value.` : ""}` : undefined,
    what: "The percentile matters more than the raw number: a reading that is ordinary for one coin is an extreme for another.",
  }),
  ctxSpread: (v) => ({
    title: "What it costs to cross from buying to selling",
    term: "Spread · best ask minus best bid, basis points",
    here: num(v) ? `${v.toFixed(2)} bps (${(v / 100).toFixed(3)}%): ${v < 2 ? "the best buy and sell prices are almost identical; a small position pays very little spread right now." : v < 10 ? "a modest spread; part of every round trip's cost." : "a wide spread; every round trip pays this before anything else."}` : undefined,
    what: "Every trade pays the spread twice, in and out. 1 basis point = 0.01%.",
  }),
  ctxBestBidAsk: () => ({
    title: "The best prices on each side right now",
    term: "Best bid / best ask",
    what: "The best bid is the highest price someone is currently offering to buy at; the best ask is the lowest price someone is offering to sell at. A market buy fills at the ask, a market sell at the bid.",
  }),
  ctxDepth: (v) => ({
    title: "How far the snapshot can see",
    term: "Order-book depth · snapshot coverage",
    here: num(v) ? `The snapshot reaches about ±${v.toFixed(2)}% from the mid price. Bands beyond that are dimmed because the figures there are incomplete, not because nothing is there.` : undefined,
    what: "Depth is how much buy and sell liquidity rests near the current price. Deeper books absorb larger trades with less price movement.",
  }),
  ctxNotional: () => ({
    title: "The visible orders, in dollars",
    term: "Bid / ask notional within the band",
    what: "Converts the resting orders on each side into dollar value so the two sides can be compared. Resting orders can be cancelled in an instant, so this is context, not a promise of liquidity.",
  }),
  ctxImbalance: (v) => ({
    title: "Which side has more waiting",
    term: "Order-book imbalance · (bid − ask) / (bid + ask)",
    here: num(v) ? `${v.toFixed(2)}: ${v > 0.3 ? "visible bid liquidity outweighs visible asks in this band." : v < -0.3 ? "visible ask liquidity outweighs visible bids in this band." : "the two sides are roughly balanced."} Resting orders change quickly, so treat this as context rather than a standalone signal.` : undefined,
    what: "Near +1 means mostly bids, near −1 mostly asks, near 0 balanced.",
  }),
  ctxWhoPays: (v) => ({
    title: "Who is paying whom",
    term: "Funding direction",
    here: num(v) ? (v > 0 ? "Positive funding: long positions are paying short positions. A modest long-side lean in perpetual positioning." : v < 0 ? "Negative funding: short positions are paying long positions. A modest short-side lean." : "Zero: neither side is paying.") : undefined,
    what: "The displayed annualized figure is what the current rate would amount to if it persisted continuously. Funding changes every eight hours, so that is a scale, not a forecast.",
  }),
  ctxMarkPrice: () => ({
    title: "The exchange's reference price for the contract",
    term: "Mark price",
    what: "Used for funding and liquidation calculations. Designed to be less sensitive to brief trading spikes than the last traded price, so a single wild print does not liquidate anyone.",
  }),
  ctxMarkVsSpot: (v) => ({
    title: "Is the futures contract trading rich or cheap to spot?",
    term: "Basis · mark price vs Binance spot, basis points",
    here: num(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(1)} bps: ${Math.abs(v) < 5 ? "the perpetual is trading almost exactly at spot; very little premium or discount." : v > 0 ? "the perpetual trades at a premium to spot, consistent with leveraged longs paying up." : "the perpetual trades at a discount to spot, consistent with leveraged shorts pressing."}` : undefined,
    what: "Funding exists to pull this gap toward zero. A persistent gap is another view of which side is crowded.",
  }),
  ctxOpenInterest: () => ({
    title: "How many contracts are open",
    term: "Open interest · in coins",
    what: "Outstanding perpetual positions that have not been closed, counted in the coin. Rising during a move means new exposure is being added; falling means positions are being closed. Direction needs other context.",
  }),

  // ---- base rates and the live condition log ----
  brCondition: () => ({
    title: "A market state that has happened before",
    term: "Condition",
    what: "Something true right now that was also true at earlier moments, such as price being below its 200-hour average or volume above twice its norm. The table asks: what happened after moments like this one on this coin?",
  }),
  brEpisodes: () => ({
    title: "How many separate times it happened",
    term: "Episodes",
    what: "A distinct historical occurrence of the condition. Adjacent bars where it stayed true are grouped as one episode, so a week-long stretch counts once rather than 168 times.",
  }),
  brHit: (h) => ({
    title: `How often price was up ${h} later, after costs`,
    term: `Hit rate, net · +${h} horizon, with a 95% confidence interval`,
    what: "The share of episodes whose forward return was positive after Wick's assumed 0.20% round-trip fee. The bracketed range is the statistical uncertainty around that share: wider means less history and less precision. It is not a 95% chance that the next outcome lands inside it.",
    formal: "Wilson score interval on the net hit rate. Grey cells have fewer than the minimum episodes to be worth reading.",
  }),
  brMedian: (v) => ({
    title: "The middle outcome",
    term: "Median net return",
    here: num(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%: half of the episodes did better than this after costs, half did worse.` : undefined,
    what: "Less swayed by a few extreme episodes than the average would be.",
  }),
  brIqr: () => ({
    title: "What a typical spread of outcomes looked like",
    term: "Interquartile range · 25th to 75th percentile",
    what: "The middle half of outcomes. It shows how varied the results were without being dominated by the wildest episodes.",
  }),
  brN: (v) => ({
    title: "How much evidence this rests on",
    term: "n · number of episodes",
    here: num(v) ? `${v} episodes. ${v < 30 ? "Too few to trust; treat the figure as an anecdote." : v < 100 ? "Enough to read, not enough to lean on." : "A reasonable sample for this coin."}` : undefined,
    what: "Small samples produce impressive-looking hit rates by accident. The confidence interval widens to say so.",
  }),
  brWalk: (v) => ({
    title: "Did the pattern survive out of the period it was found in?",
    term: "Walk-forward · first year vs second year of history",
    here: Array.isArray(v) && num(v[0]) && num(v[1]) ? `${(v[0] * 100).toFixed(0)}% in the first year, ${(v[1] * 100).toFixed(0)}% in the second. ${Math.abs(v[0] - v[1]) > 0.15 ? "A large difference: the pattern may not be stable over time, or it was fitted rather than found." : "Similar in both halves, which is what a stable pattern looks like."}` : undefined,
    what: "Splitting the history in two is the cheapest honest test. A pattern that only worked in one half is a warning.",
  }),
  brGross: () => ({
    title: "Before trading costs",
    term: "Gross hit rate and median",
    what: "The same figures before Wick's assumed 0.20% round-trip taker fee. The gap between gross and net is what costs eat on short horizons.",
  }),
  brOverlap: (_v, detail) => ({
    title: "These observations are not independent",
    term: "Overlapping windows",
    here: detail ? `${detail}: episodes start closer together than the horizon they are measured over, so consecutive outcomes share the same bars.` : undefined,
    what: "When forward-return windows overlap, the effective sample is smaller than n suggests and the confidence interval is too narrow.",
  }),
  brHorizon: (h) => ({
    title: `What price did ${h} after the condition appeared`,
    term: `Forward return · +${h}`,
    what: "Net of the assumed round-trip cost. Dots mean the window has not finished yet.",
  }),
  logOos: () => ({
    title: "Recorded before the answer was known",
    term: "Out-of-sample · live condition log",
    what: "The base-rate table was discovered on the same history it summarizes, so patterns can look stronger there than they will on unseen data. This log writes each condition down as it becomes true and fills in the result later, which is the honest test.",
  }),
  logOnsets: () => ({ title: "New occurrences since live tracking began", term: "Onsets", what: "Each time the condition flipped from false to true on a closed hourly candle." }),
  logResolved: () => ({ title: "Old enough for the full result to be known", term: "Resolved at 24h", what: "Onsets at least 24 hours old, so the +24h return is final." }),
  logPositive: () => ({ title: "How many ended positive after costs", term: "Positive net", what: "The share of resolved onsets whose 24-hour return was positive after the assumed round-trip fee. Compare with the historical hit rate above." }),

  // ---- chart guides ----
  chartUptrend: () => ({
    title: "Higher highs and higher lows",
    term: "Uptrend structure",
    what: "Each pullback stopped above the previous one and each push went further than the last: buyers are accepting progressively higher prices. This supports an uptrend reading; it does not guarantee continuation.",
  }),
  chartDowntrend: () => ({
    title: "Lower highs and lower lows",
    term: "Downtrend structure",
    what: "Each bounce stopped below the previous one and each drop went further: sellers are accepting progressively lower prices. It describes what has happened, not what must happen next.",
  }),
  chartRange: () => ({
    title: "Going nowhere, for now",
    term: "Range",
    what: "No clean sequence of highs and lows. Price is oscillating between levels that have held. Ranges resolve eventually; the direction is not implied by the range itself.",
  }),
  chartSupport: (v) => ({
    title: "Where buyers have stepped in before",
    term: "Support · cluster of swing lows",
    here: num(v) ? `${v > 1 ? `Touched ${v} times: ` : ""}price has stopped falling around here before. Wick treats support as an area, not an exact price.` : undefined,
    what: "A level where selling has been absorbed previously. If it breaks, the buyers who defended it are no longer holding, which is why breaks matter more than the level itself.",
  }),
  chartResistance: (v) => ({
    title: "Where sellers have shown up before",
    term: "Resistance · cluster of swing highs",
    here: num(v) ? `${v > 1 ? `Touched ${v} times: ` : ""}price has struggled to trade above here before. An area, not a magic number.` : undefined,
    what: "A clean break with strong participation matters because the sellers who defended the level are no longer holding it.",
  }),
  chartBreakout: (v) => ({
    title: "Price has left the range to the upside",
    term: "Breakout",
    here: v === true ? "On elevated volume, which is what distinguishes a meaningful breakout from a brief poke through the level." : "Without a volume expansion. Breakouts on thin volume fail more often; Wick looks for volume, momentum, flow and follow-through before believing one.",
    what: "Price has moved above a level that previously contained it.",
  }),
  chartBreakdown: (v) => ({
    title: "Price has left the range to the downside",
    term: "Breakdown",
    here: v === true ? "On elevated volume: participation is behind the move." : "Without a volume expansion, which makes a failed breakdown more likely.",
    what: "Price has moved below a level that previously held it.",
  }),
  chartCompression: (v) => ({
    title: "Volatility has gone quiet",
    term: "Compression",
    here: num(v) ? `The last 20 bars cover only ${Math.round(v * 100)}% of the range before them. A big move usually follows a squeeze; the direction is not implied.` : undefined,
    what: "Ranges contract before they expand. Compression says a move is coming, not which way.",
  }),
  chartExtension: (v) => ({
    title: "Stretched from its recent average",
    term: "Extension · distance from the 20-bar mean, in ATR",
    here: num(v) ? `${Math.abs(v).toFixed(1)} ATR ${v > 0 ? "above" : "below"} the 20-bar mean. Much larger than normal short-term movement, so entering here carries more risk of chasing; pullbacks toward the mean are common from here.` : undefined,
    what: "Price tends to return toward its recent average. The further it is stretched, the worse the entry, whatever the direction.",
  }),
  chartPullback: () => ({
    title: "Moving against the trend, temporarily",
    term: "Pullback",
    what: "Price is back at its 20-bar mean inside a trend. A controlled pullback can offer a better entry than chasing an extended move, provided the structure of higher lows (or lower highs) remains intact.",
  }),
  shapeName: (label, name) => ({
    title: name ? String(name) : "Shape",
    term: "3-day shape · from the hourly candles",
    what: typeof label === "string" && SHAPE_PLAIN[label] ? SHAPE_PLAIN[label] : "The geometry of the last three days, given a name. Shapes describe what happened; they do not predict.",
  }),

};

function interpretMove(v: number, ctx?: Ctx): string {
  const a = Math.abs(v);
  const size = a < 0.5 ? "within ordinary daily noise" : a < 1 ? "a normal day's worth of movement" : a < 2 ? "more than a typical day" : "far outside this coin's normal short-term movement";
  const tail = a > 1 ? " Entering here carries more risk of chasing an already-extended move." : "";
  const pctTxt = num(ctx?.atrPct) ? ` (about ${(a * ctx!.atrPct!).toFixed(1)}% when a typical day is ${ctx!.atrPct!.toFixed(1)}%)` : "";
  return `${sym(ctx)} has moved ${v >= 0 ? "+" : "−"}${a.toFixed(2)} ATR since yesterday's close${pctTxt}: ${size}.${tail}`;
}

const compact = (x: number) => Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(x);

/** Plain-English reason for the first failing check, using the check's own detail. */
export function explainConcern(name: string, detail?: string): string {
  const d = detail ? ` (${detail})` : "";
  if (name.startsWith("Trend") || name.startsWith("Larger trend")) return `No clear direction — price is not consistently on one side of its key moving averages, so there is nothing to join${d}.`;
  if (name.startsWith("Volume")) return `Activity is ordinary — the move has not drawn enough participation to confirm it${d}.`;
  if (name.startsWith("Funding")) return `Funding is crowded — one side is already paying to hold, which raises squeeze risk${d}.`;
  if (name.startsWith("RSI")) return `Momentum is stretched — price has moved unusually one-sidedly, so entering here often means chasing${d}.`;
  if (name.startsWith("Move")) return `The move is small — within what this coin does on an ordinary day, so it may mean nothing yet${d}.`;
  if (name.startsWith("Taker") || name.startsWith("Flow") || name.startsWith("Aggressors")) return `The urgent side disagrees — the people crossing the spread are on the other side of the trend${d}.`;
  if (name.startsWith("Shape") || name.startsWith("Not an exhaustion") || name.startsWith("Exhaustion")) return `The recent shape argues against it — this geometry has more often exhausted than continued${d}.`;
  if (name.startsWith("Pulled back")) return `No pullback yet — price has not come back to its 20-bar mean, so the entry would be a chase${d}.`;
  if (name.startsWith("Own move")) return `Not moving on its own — most of the move is the market, not this coin${d}.`;
  return `${name}${d}`;
}

/** Friendlier one-liners for the shape labels. Observation, not prediction. */
export const SHAPE_PLAIN: Record<string, string> = {
  blowoff_up: "Price has accelerated unusually fast on elevated activity. Moves like this can indicate exhaustion, but strong momentum can persist longer than expected.",
  capitulation: "An unusually vertical drop on extreme volume with momentum pinned low. Forced selling; sometimes followed by a bounce, sometimes by more selling.",
  flag_up: "A big rise, then a tight, quiet range. Textbooks expect continuation higher; this coin's own base rate is shown below.",
  flag_down: "A big drop, then a tight, quiet range. Textbooks expect continuation lower.",
  dead_cat: "A hard fall followed by a partial bounce. Could be a real recovery or a pause before more selling; volume on the bounce decides.",
  v_reversal: "A hard fall that was mostly bought back quickly. Sellers were absorbed fast.",
  trend_up: "A clean, efficient rise with dips bought. A momentum regime.",
  trend_down: "A clean, efficient decline with rallies sold.",
  squeeze: "Volatility has compressed well below normal. A big move usually follows; the direction is not implied.",
  range: "Lots of motion, no net progress. The edges have held and breakouts have failed.",
  chop: "No recognisable pattern. Noise.",
};
