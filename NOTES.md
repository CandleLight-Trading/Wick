# NOTES: design decisions

Read this alongside the code. The comments in each module explain the *how*; this file
explains the *why* for the decisions that were not obvious.

## Architecture

```
Binance WS  ──> IngestService ──> Store (ring buffer + SQLite) ──> Broadcaster ──> browser /ws
Binance REST ──> Backfiller ────────────^
Binance REST ──> ticker poll (30s), depth poll (10s, only while a Context panel is open)
```

- **One WebSocket to Binance**, all tracked streams multiplexed on it via the combined
  stream endpoint. When the tracked set changes we send `SUBSCRIBE`/`UNSUBSCRIBE` on the
  live socket instead of reconnecting. 20 symbols x 6 intervals + 20 tickers = 140 streams,
  roughly 80 messages/second. Binance allows 1024 streams per connection.
- **Everything Binance-specific lives in `adapters/binance.py`.** Stream names, casing,
  positional kline arrays, string prices, the `k.x` flag. Nothing downstream imports it.
  A Kraken or ccxt adapter implements `ExchangeAdapter` and is swapped in `main.py`.
- **The frontend never triggers a REST call to Binance.** Every `/api/*` handler reads
  memory or SQLite. A curious browser cannot get your IP banned.

## Unclosed candles (the bug that silently ruins datasets)

A kline stream re-sends the forming candle every ~2 seconds with `k.x == false`, then once
more with `k.x == true` when it closes. Appending each message produces dozens of copies
of every candle. Nothing crashes; RSI, ATR, volume averages and base rates are all quietly
wrong from then on.

`candle_state.py` is a pure state machine, one instance per (symbol, interval), that
classifies each frame as UPDATE (overwrite forming candle), CLOSE (overwrite and mark
final), NEW (a later open_time appeared) or REJECT_STALE. The store upserts on the unique
index `(symbol, interval, open_time)`, so even a duplicate close frame is harmless.

"Stale" is defined against the kline's own `open_time` and the exchange's event time `E`,
never wall clock:

- earlier `open_time` than the tracker's current candle: reject
- same `open_time`, older `E`: reject (a late frame after a reconnect)
- same `open_time`, tracker says closed but frame says forming: reject
- later `open_time`: NEW, and report the gap (see below)

Wall clock is used only for the UI's stale indicator (no frame in >10s while "live").

## Reconnect and gap repair

Binance drops every connection after 24h, and networks hiccup. The WebSocket does not
replay what you missed. So:

1. Reconnect with exponential backoff and jitter (1s, 2s, 4s ... capped at 60s, times a
   random 0.5-1.5). Jitter matters: without it every client on the internet reconnects in
   the same second after a Binance-side blip.
2. On every connect, `Backfiller.sync_recent` compares the last stored *closed* candle
   with now and fetches the missing range via REST `/klines`, paginated in 1000-bar pages.
   This runs concurrently with the live feed rather than before it, so a slow backfill
   never lets the socket's receive buffer fill up. Concurrent writes are safe because
   SQLite is the source of truth and the ring is rebuilt from it after a bulk write.
3. The live state machine also detects gaps *without* a disconnect. When a frame arrives
   with a later `open_time`, the gap is the whole span from the last known candle to the new
   one. If the previous candle never received its close frame, the span starts at that
   candle so it gets refetched too. A jump of 50 candles queues one job covering all 50.
4. Genuine repairs (not first loads) are logged at WARNING and shown in `/api/status`.

The tracker deliberately forgets everything on reconnect. The store knows history; the
tracker only needs to know the current candle.

## Ping/pong

The `websockets` library answers server pings automatically in its protocol layer
(`websockets/protocol.py`: an incoming `OP_PING` frame queues an `OP_PONG` with the same
payload). We also send our own ping every 20s and drop the socket if no pong arrives in
20s, which is our liveness check. To *verify* rather than trust this, set
`LOG_WS_FRAMES = True` in `config.py`: the library's DEBUG log prints every ping received
and pong sent.

## Rate limits

Binance's REST budget is weight-based, per IP, 1200/minute. `RestClient` reads the
`x-mbx-used-weight-1m` header on every response and sleeps until the next minute once
usage reaches 900. HTTP 429 backs off for `Retry-After` seconds and retries; HTTP 418
means the IP is banned, so the client refuses every subsequent call and the UI shows a
red banner. Steady-state budget:

| source | weight/min |
|---|---|
| 24h ticker poll, weight 80 every 30s | 160 |
| depth poll, weight 50 every 15s, per open Context panel | 200 |
| total | ~360 |

First-run backfill is about 28 calls per symbol (18 for two years of 1h bars, 2 for 1500
days of 1d bars, 8 for the other intervals), roughly 1100 weight for 20 symbols, spread
so no minute exceeds 900. It takes about two minutes. Transport errors (DNS, connect,
read timeout) are retried inside `RestClient` with exponential backoff, and the whole
sync pass retries itself, because a laptop waking from sleep produces exactly that
sequence and a sync that gives up until the next reconnect could wait 24 hours.

## Timestamps

Binance uses milliseconds. lightweight-charts wants seconds. Internally everything is
milliseconds; `timeutil.ms_to_s` is the only conversion and `Candle.to_wire` is the only
caller on the data path. `test_timeutil.py` pins it. The frontend never divides by 1000.

Daily candles close at 00:00 UTC. The ticker's "24h change" is a rolling window ending
now. The two disagree and both are right; the UI labels them "24h rolling" and "1d UTC",
and charts render in UTC.

## Backpressure

Each browser client has a bounded queue (200 frames). When it fills, the oldest frame is
dropped and a counter increments; the status badge shows the count. A chart only ever
wants the newest state of a candle, so dropping a stale update costs nothing.

## Why the Context panel does not emit signals

A BUY/SELL verdict, a composite score or a traffic light compresses several noisy numbers
into one confident-looking token and hides how weak the evidence is. The panel instead
shows each number with the context needed to read it: volume as a multiple of this
symbol's own 30-day average, moves in units of this symbol's own ATR, RSI against this
symbol's own historical distribution, and correlations so eight coins at 0.9 are visibly
one position.

The base-rate strip is the honest version of a signal. For every condition true right now
it shows how often it occurred and what happened afterwards. Several things keep it honest:

- **Episodes, not bars.** Consecutive true bars collapse into one onset, so "RSI < 30 for
  six hours" counts once. Displayed n is episode count.
- **Net of costs by default.** Every forward return has 0.20% (Binance spot taker, both
  sides) deducted. A +0.11% median at +4h renders as a loss, because it is one.
- **Intervals, not points.** Hit rates carry a 95% Wilson interval. "54% [46-62%]" reads
  differently from "54%".
- **Overlap flag.** When the median gap between episodes is shorter than the horizon,
  outcomes share bars and the row says so.
- **Multiple-testing count.** The strip states how many symbol x condition x horizon
  combinations exist (currently 20 x 6 x 3 = 360). At that count several rows will look
  excellent purely by chance.
- **n < 30 is greyed out.**

Two caveats that are documented rather than fixed:

- **Episodes cluster.** RSI < 30 onsets bunch during drawdowns because volatility clusters.
  Five hundred episodes over two years might represent fifteen distinct regimes. The
  Wilson interval assumes independence, so read it as a floor on the uncertainty.
- **The tracked symbols are survivors.** They were chosen for being liquid today. Two years
  of history on coins selected by present-day liquidity is biased upward, and two years of
  crypto is roughly one regime.

## The condition log

`conditions.py` records every condition onset as 1h candles close, then fills in the
+1h/+4h/+24h returns as those bars close. It uses the exact same condition definitions as
the base-rate strip, but it is written *before* the outcome is known. After a month, the
comparison between the in-sample strip and this out-of-sample log is the only evidence
available on whether any of it means anything. It logs conditions, not recommendations.

## Phase 2: perpetual futures positioning

Funding is the one number on the Context panel that is not derived from price. A perp
has no expiry, so the exchange keeps its price near spot by making one side pay the
other every funding period. Persistently positive funding means longs are crowded and
paying to stay in; that is positioning, not an indicator.

`futures.py` has two sources behind a three-method interface:

- **BinanceFutures** (`fapi.binance.com`), as specified: `premiumIndex` for mark, index
  and the last funding rate for every symbol in one weight-10 call, `openInterest` per
  symbol, `fundingRate` for the 8-hourly history. Binance answers HTTP 451 from
  restricted locations, which is what happened on the machine this was built on.
- **KrakenFutures** (`futures.kraken.com`), the fallback: one public `tickers` call has
  mark, index, open interest and funding for every contract. Kraken quotes funding as an
  absolute amount per contract per hour; we divide by mark and multiply by eight so both
  venues display a fraction per 8h. Kraken perps are USD-margined (`PF_XBTUSD`, still
  XBT), so the "mark vs Binance spot" basis includes the USDT/USD rate.

`FuturesPoller` tries sources in order and advances on 451/403, refreshing every minute
for all tracked symbols and pulling seven days of history every five minutes for the
symbols with a Context panel open. The status badge says which source is live.

## Phase 2: second exchange (Kraken spot)

`adapters/kraken.py` implements the same `ExchangeAdapter` and runs as a second
`IngestService` with tickers only. Nothing in ingest, store, broadcaster or the API had
to learn what Kraken is; the adapter boundary held. What the adapter had to absorb:

- Kraken REST v0 uses legacy names (`XBTUSDT`, `XXBTZUSD`, wsname `XBT/USDT`); WebSocket
  v2 uses `BTC/USDT`. The v2 API renamed XBT to BTC and XDG to DOGE in July 2026, so
  older examples online are wrong. Canonical symbols are Binance-style; per-venue names
  come from the `AssetPairs` response fetched at boot, never guessed (a first version
  guessed the quote from the string and died on `ADAAUD`).
- No combined-stream URL: connect, then send one subscribe message per channel
  (`subscribe_on_connect`). This needed a two-line change in `IngestService` and a
  signature change from one subscribe message to a list.
- OHLC frames carry no closed flag. A candle is only final when its successor begins.
  The adapter marks every live OHLC frame unclosed, which makes the state machine refetch
  each candle when the next one starts: correct, chatty, and why Kraken runs tickers only.
- Kraken's ticker "change" is since 00:00 UTC, not a rolling 24h window. Labelled as such.
- No weight header; the REST client enforces one request per second instead.

Cross-exchange divergence shows in the Market table (Kraken minus Binance, basis points)
and on the Context panel next to the live spread. Most of the time it is smaller than the
round-trip fee, which is the point of showing the two side by side.

## Phase 3: the Analysis tab (and why it is allowed to say "long")

The Context panel never emits a verdict. The Analysis tab does, and the reason it can
without betraying the rest of the app is that every verdict is written to the
recommendation log before the outcome is known and paper-traded at fixed size. The
scoreboard at the top of the tab, net of fees and funding, is the only evidence that
either judge is worth listening to. If it is not, the judge is wrong, not the market.

Two judges, side by side on every card:

- **Rules** (`scanner.py`): trend from price vs 20/50/200 MA on 1h bars, volume at least
  1.3x the 30-day average, funding not crowded (above +0.03%/8h longs are paying up),
  RSI not at an extreme, and a move of at least half a daily ATR in the direction of the
  trend. Every check is shown with pass/fail. Deterministic, free, runs every 15 minutes.
- **Model** (`llm.py`): one OpenAI Responses API call with web search per symbol. It
  receives the same numbers, the rules verdict, the base rates and the judge's own track
  record, and must fill a strict JSON schema: stance, confidence, horizon, invalidation,
  trend type, drivers with URLs, risks, numbers used. The prompt tells it the prop-firm
  constraints so the horizon and stop are sized for a daily loss limit.

Cost control: the scanner (no model) ranks every tracked coin by how unusual its state
is for that coin, in ATR and volume-multiple units. Only the top 5 get a model call, a
symbol is not re-researched within 4 hours, and a hard cap of 25 calls per day is
enforced from the analyses table. With `search_context_size: low` that is roughly 25
cents a day. Clicking "research now" counts against the cap.

Horizon: prop-challenge rules (daily loss 3-5%, max drawdown 8-10%, weekend gaps,
funding bleed on multi-week holds) and the failure statistics (71% of first-phase
failures are daily-limit breaches from overtrading) point to 12-72 hour holds. Both
judges default to 48h and every call carries an invalidation price 1.5 daily ATRs away,
plus the notional that keeps a stop-out at the configured 1% daily loss budget.

Paper trading (`paper.py`): fixed $1,000 notional per call on a $10,000 account; exit on
invalidation touch (candle low/high), horizon expiry, or a stance flip; P&L net of the
0.20% round trip and funding at the entry rate. The scoreboard reports hit rate, equity,
max drawdown, worst day and best-day share, because those are the numbers a prop
challenge actually grades. This is the seed of the simulated prop account section.

The key is read from `OPENAI_API_KEY` or `backend/.env` (gitignored) and never leaves
the backend. The model id is verified against `/v1/models` at boot so a wrong name shows
up as a readable error on the tab instead of a 404 on the first call.

## The shape engine (what "chart patterns" look like as numbers)

Patterns drawn by eye (head and shoulders, cup and handle) are subjective and the
evidence that they predict anything is thin. What can be measured is the geometry those
names point at. `shape.py` computes, for every bar of the 1h history:

- regression slope and R^2 of log price over 24 and 72 bars (direction, straightness)
- Kaufman efficiency ratio over 72 bars (net move divided by total path)
- 24-bar range, 72-bar drop and retrace fraction, and whether the high or the low came
  first (the geometry of a dead-cat bounce versus a V)
- 20-bar bandwidth against its own 30-day average (squeeze)
- last-24h volume against the 30-day hourly average (climax)
- RSI, and, for the current bar only, the Lo-MacKinlay variance ratio at 4 and 24 bars
  and lag-1 autocorrelation (momentum versus mean reversion)

Everything is in units of one typical day's range for that coin (hourly ATR(14) times
sqrt(24)), so the same thresholds mean the same thing for BTC and a memecoin. A priority
list turns the numbers into one label per bar: blow-off, capitulation, bull/bear flag,
V-reversal, drop-then-partial-bounce, clean up/down trend, squeeze, range, or chop.

The honest part: the current label's own history on this symbol goes through the same
onset and forward-return machinery as the base-rate strip. "Bull flag" therefore comes
with "n=41 episodes on this coin, 54% [39-68%] positive at +24h" rather than a textbook
promise. The rules judge uses the shape as a veto only: a blow-off or capitulation bar is
exhaustion, and a shape whose base rate on this coin is clearly adverse blocks the call.
The model judge gets the shape and its base rate in the evidence pack.

Rolling statistics use prefix sums and an incremental Sxy (shifting the window turns
every x into x-1, so Sxy_new = Sxy_old + w*y_new - Sy_new), and sliding max/min use a
monotonic deque. The whole 17,500-bar history costs one pass per statistic, a few hundred
milliseconds in pure Python, run off the event loop.

## Phase 4: flow, liquidity, sizing, alerts, implied vol

Everything here is public and free; it is what a professional would add first.

- **Taker buy share.** Binance klines carry the volume bought by aggressors (column 9,
  `k.V` on the stream). Its share of total volume is order flow without a trade stream.
  Stored in a new `tb` column (migrated in place, back-filled with one REST page per
  symbol). The rules judge now requires flow on the trend side.
- **Slippage from the book.** `walk_book` fills a notional level by level against the
  1000-level snapshot and reports the average price versus mid in basis points, at $1k,
  $10k, $50k and your configured account size. A flat 0.20% cost is fine for BTC and
  wrong for thin pairs; this number is not. The scanner fetches depth for the top movers
  (weight 50 each, every 15 minutes).
- **Beta and residual.** 60-day daily-return beta to BTC, and the coin's 24h change minus
  beta times BTC's: how much of the move is the coin's own.
- **Breadth.** Share of tracked coins above their 20/50/200 MA and up on the day. A coin
  breaking out while breadth is 90% is riding the market; at 20% it is on its own.
- **Walk-forward.** `forward_stats` now reports the hit rate on the first half of history
  and on the second half separately, everywhere a base rate is shown. If the second half
  is much worse, the pattern was fitted, not found. This is the cheapest honest
  validation available without holding data out entirely.
- **Sizing.** Two numbers on every call: size by stop (a stop-out costs the daily budget)
  and size by volatility (an EWMA, lambda 0.94, one-sigma day costs the daily budget).
  When they disagree the stop is either too tight or too wide for the coin's volatility.
- **Prop tracker.** Today's paper P&L, realised plus unrealised, against the configured
  daily loss limit and max drawdown, with the same bars a prop dashboard shows.
- **Alerts.** In-app feed always; Discord webhook and Telegram bot if configured in
  `.env`. Fired when a coin enters the top movers, a judge opens a call, or a paper
  position closes. Keys stop repeats across rescans and restarts.
- **Implied vol.** Deribit's public DVOL index for BTC and ETH, against our realised 30d
  vol, gives the variance risk premium. Positive is normal; negative is worth noticing.

Not added because it is not freely available from here: liquidation streams, long/short
ratios and open-interest history (Binance futures is geo-blocked), on-chain and exchange
net flows (paid providers), social sentiment (paid X/Reddit APIs).

## Phase 5: setups, the ticket, and Prop (the loop)

The product principle: Wick does the work, you make bounded decisions, and the position
loop is the payoff. One verb per stage: RESEARCH, BUILD TRADE, OPEN POSITION, REVIEW &
CLOSE. Analysis stays the research workspace; Prop is where money lives.

Two workflows that meet at BUILD TRADE (`desk.py`):

- **Setups** are Wick's opinion about a market situation and never know about money.
  `SCANNED -> RESEARCHED -> PASSED / EXPIRED`. Created for top movers and non-flat rules
  stances, with STRONG / MIXED / WEAK from the share of passing checks and the first
  failing check as the "main concern". A model analysis newer than the setup counts as
  research; the recommendation is the model's `action`: ENTER / WAIT / PASS. An
  unresearched setup can be PROMISING but never ENTER; its button is RESEARCH.
- **Trades** belong to an account. `WAITING -> READY -> OPEN -> CLOSED` (or OPEN at once,
  or CANCELLED). One setup can produce trades in several accounts, or none.

Planner v1 (`planner.py`), recorded on every trade as `planner_version`: the stop is the
48-bar swing on the setup's timeframe plus a 0.3 typical-day buffer; the target is the
nearer of the 7-day extreme and the shape's median favorable excursion on this coin,
each only if at least one typical day beyond entry (price at the week's high would
otherwise "target" the next tick). R:R is computed last; under 1.5 the ticket says PASS
but is takeable under Advanced. Wick advises; the trader decides.

Account rules are the only hard block: a 3% cap on portfolio open risk and today's
remaining daily loss budget. Risk profiles (0.5 / 0.75 / 1.0%) change size only, never
the stop, because the market does not care about your risk preference.

WAIT plans never open themselves. The monitor checks 1-minute candles every minute and
flips the trade to READY; opening is your click. A stop is an order you placed, so it
fills automatically at the stop price (STOP FILLED), as a real prop account would. A
target is flagged (TARGET REACHED); closing is your click. Other flags: STOP NEAR,
STRUCTURE CHANGED (shape flipped against the position), HORIZON ENDING.

Prop shows equity, return, target progress, and the four bars a prop dashboard shows:
daily loss used, drawdown used, open risk used, target reached. Track record per account
in R, by shape at entry, and the you-versus-judges tally, where the judges' R comes from
the recommendation log's P&L over its stop distance.

Deferred on purpose: Call Your Shot, MFE/MAE and the full postmortem rubric, RESEARCH
AGAIN on open positions, and the interpretation layer. A week of real use decides.

## Phase 5b: agency pass (manual trades, the account graph, typography)

Real use exposed the guided loop as slightly authoritarian. Corrections:

- **A trade does not require a setup.** `trades.setup_id = 0` means manual. The ticket
  opens from Market rows, from the Charts toolbar, from the Prop search box and top movers,
  and from every Analysis card. Untracked coins start streaming the moment a ticket opens;
  until 200 bars exist the planner falls back to a 5% volatility stop and no target, says
  PASS, and lets you take it.
- **TAKE IT ANYWAY works.** Wick's PASS and "not researched yet" are a soft gate: the API
  refuses without `force`, and the UI's amber button sends `force`. Only the account rules
  (open-risk cap, daily loss budget, breached status) disable the button. Targets are
  nullable: "none · manage it yourself".
- **ANALYZE** from Market or Charts tracks the coin if needed, rescans immediately, and
  lands on Analysis with that coin first and highlighted.
- **Prop is an account, not a report.** Equity per account is snapshotted every minute
  (open P&L included) into `equity_snapshots`, and the hero draws it with 1D/1W/1M/ALL.
  Analytics moved below positions and history. Multiple accounts everywhere: the ticket
  picks the account, the Prop selector switches the whole view.
- **Typography.** Tailwind sizes are rem-based, so one root `font-size` (16px) sets the
  scale; the old 10-11px metadata is now 12-13px and body 14-15px. A Comfortable/Compact
  toggle in the header flips the root to 13px for Bloomberg density.

## Phase 5c: sizing as a recommendation, costs, the ledger

- **Wick recommends, you choose.** The ticket defaults to Wick's size: the position whose
  stop-out costs the risk profile's share of equity (0.5 / 0.75 / 1.0%). "Choose my amount"
  opens a slider with Wick's marker on it and the account's true ceiling at the end: the
  largest position whose stop-out still fits both the open-risk cap and today's remaining
  loss budget. Above the recommendation is a warning and allowed; above the ceiling is
  capped, with the reason stated. Sizing from the loss at invalidation, not from an
  attractive dollar figure, is the one habit worth training before a real challenge.
- **Exposure, not margin.** Size is quoted as position notional. Leverage and margin are
  not modelled yet; P&L is computed on the full exposure, as a perp would.
- **Costs are itemised.** A closed trade stores gross move, taker fees both ways, funding
  for the hours held, and net. Slippage is already inside the entry price from the book
  walk. The review screen shows the breakdown; the ledger exports it.
- **Ledger.** `GET /api/accounts/{id}/ledger.csv`: every trade, every cost, Wick's call,
  whether you overrode it, the thesis, and your notes. Notes are edited on the review
  screen and stored on the trade.
- Accounts can be renamed and deleted (never the last one). The Prop tab is labelled
  Trade in the nav.

## Research provenance and cost (phase 5d)

The flow, made explicit because it was opaque:

    Market (free, continuous)  ->  Analyze / auto-promote  ->  Analysis: setup + live signal (free)
    -> you click RESEARCH  ->  one model call, cached with a timestamp  ->  Wick view  ->  Trade

- **Nothing calls the model on its own.** `AUTO_RESEARCH = False`. Scanning, rules, shapes
  and setups keep running for free. The model runs only on RESEARCH / REFRESH RESEARCH.
- Every card shows three things side by side: the live signal (rules judge, updates every
  scan), the research state with its age and who triggered it, and Wick's view. Research
  is CURRENT under 2h, AGING to 6h, STALE after, or STALE immediately if the market moved
  more than one daily ATR since, the shape changed, or the quant signal flipped. Stale
  research turns the button into REFRESH RESEARCH and the Wick view into "refresh before
  acting". It is never refreshed automatically.
- The header counts setups by state and shows the AI usage: calls today and an estimated
  cost from token counts plus one web search per call. Clicking it lists each call with its
  trigger. Estimate rates live in `config.py`.
- The word "paper" is gone from the UI. Accounts are accounts.

## Analysis simplified (phase 5e)

Three concepts only: the quantitative setup, the AI research state, and Wick's verdict.
Three sections: Needs Research (the work queue), Researched (with age and staleness),
and a collapsed Watching list for tracked coins that are not a setup right now. Trade
states (waiting, ready, open) live on the Trade tab; Analysis only links to positions
that need attention.

One dominant action per card: unresearched -> RESEARCH; stale -> REFRESH RESEARCH;
researched ENTER -> BUILD TRADE; WAIT -> VIEW PLAN; PASS -> DISMISS SETUP. "Trade Anyway"
and "Clear Research" are quiet secondary actions. The old "Details" toggle is "Quant
Details"; the three boxes are Quant Signal, AI Research, Wick Verdict.

Clear Research (per setup, or all from the ••• menu) removes only the cached model
conclusion and records `researchClearedAt` so an older analysis cannot re-attach.
Reset Analysis Workspace expires every active setup and rescans. Neither touches candles,
accounts, trades or the usage log.

Analysis uses tabs (Needs Research / Researched / Watching) rather than stacked sections.
A watched coin can be pushed into the queue by hand ("Add to Research Queue"): the setup
is created pinned, so the scanner keeps it active regardless of its own opinion until
you dismiss it. Every popup closes on a click anywhere outside it, never on hover.

## Clarity pass (phase 5f)

- **Action first, everywhere.** Quant Signal, AI Research, Wick Verdict and the position
  review all start with what to do and one plain reason: "WAIT — do not enter yet. ...",
  "AVOID FOR NOW — research weakens the trade.", "HOLD — ...". Details follow.
- **Glossary and delayed tooltips.** `glossary.ts` explains every term in plain English
  plus what the current value implies here. `Term` shows it after a sustained 2.5 s hover
  so skimming the page does not spawn popups. Main concern is spelled out as a sentence.
- **Closing is never one click.** The close ticket offers 25/50/75/100% or an amount,
  previews realized P&L (fees and funding included), remaining exposure and remaining
  risk, and needs a Confirm. Partial closes book the closed slice as its own closed row.
  "Liquidation" is reserved for forced closes; the UI says Close and Partial Close.
- **Research on open positions.** One manual model call with the entry thesis and
  snapshot, current conditions and the local "what changed"; returns HOLD / WATCH CLOSELY
  / REDUCE / CONSIDER EXIT with a reason. Advisory only.
- **No daily cap.** Calls are counted, never limited; the in-flight guard stops doubles.
- Compare mode on the Trade graph overlays up to 8 accounts as percent return.

## Phase 6: persistence and hosting

Goal: the desk keeps running when the laptop is closed, at a public HTTPS URL, with one
person's password in front of it. Railway Hobby, one service, SQLite on a volume. The
plan went through a ChatGPT review; the seven corrections it produced are all in here.

- **Probe before hosting, never relocate to evade.** `scripts/probe.py` checks the
  official endpoints from the host region. Binance spot works from Jason's location and
  fapi does not (451), which is why funding already comes from Kraken Futures. If spot
  fails in a region the answer is Kraken-as-primary, not a proxy.
- **Auth is one password done properly.** HMAC-signed session in an HttpOnly, SameSite=Lax
  cookie (Secure in production); every `/api` route and `/ws` need it; mutating requests
  and the socket must carry our own Origin; five failures a minute per address throttle
  login; an epoch bump logs out everywhere; tokens expire in 30 days. `/healthz` is the
  only unauthenticated route and says nothing beyond up/degraded and ready. Production
  refuses to start without `WICK_PASSWORD`. Locally with no password, nothing changes.
- **ALIVE is not READY.** The process serves pages at once; `history_ready` fires when
  stored history is continuous (shallow sync, deep sync, hole repair). Until then, plus
  the reconciliation below, the first scan waits and research/trade actions return 503
  with a plain sentence; the UI shows a sync banner with progress. Live frames and
  backfill overlap safely because every write is an upsert keyed on open time.
- **Downtime reconciliation never invents order.** At startup, after history is ready,
  1m candles are pulled for the life of every open or pending trade (the exchange serves
  1m arbitrarily far back), then `monitor(replay=True)` walks the stored tape in order.
  The first bar that crosses the stop fills it at the stop price, timestamped at that
  bar's close (`at_ms`), not "now". If the same bar also reached the target, the order
  inside the bar is unknowable: the stop is assumed first and the fill is recorded as
  `ambiguous`, with the resolution used (1m, or 5m/1h where 1m was missing). Waiting
  trades whose trigger was touched become READY only; nothing opens itself and no
  research runs unasked. The same replay runs every minute in normal operation, now
  reading SQLite instead of the 25-hour ring, so a laptop sleep of a day behaves the same
  as a redeploy.
- **Retention protects money, not curiosity.** 1m seven days, 5m sixty, 15m one-eighty,
  1h and slower kept. Symbols with an open or pending trade keep every bar back to a day
  before the trade. Setups are self-contained snapshots and pin nothing. Equity snapshots
  thin to five-minute spacing after a week.
- **Migrations are numbered.** `schema_version` table; the two ad-hoc ALTERs became
  migrations 1 and 2, tolerant of databases that already had the columns.
- **Exactly one worker.** Rings, fan-out and the SQLite writer are per-process; the
  Dockerfile and railway.json both pin one replica and `--workers 1`.

Measured on the laptop during the boot sync: about 200 MB RSS, well inside Hobby.
Deferred: a backup script (SQLite backup API, rolling seven, optional bucket) once the
deployment has run a week; a Logout button (the API exists).

## Phase 6b: speed and momentum

- **Scan was 25 s because of shapes.** Every scan re-read 17,500 hourly bars per coin and
  re-labelled them (about 0.6 s each, 36 coins). A shape only changes when an hourly candle
  closes, so the report is now cached per last closed 1h open time. Warm rescans take
  about a second; adding a coin from Market takes about four (its shallow backfill, then
  one scan), where before it waited for the next 15-minute scan or contended with the
  boot sync for the REST lock. Depth for top movers is refreshed at most every five minutes.
- **Analysis lists newest first** and every card says when it was added.
- **Market momentum columns.** 1h % is the first derivative (change over the last hour);
  Accel is the second (this hour's change minus the previous hour's). Tracked coins use
  closed 1m candles; every other USDT pair uses minute-spaced samples from the ticker poll,
  memory only, so those columns take an hour or two to fill after a restart.

## UI conventions (enforced from phase 5c on)

- Navigation, buttons and section headings: Title Case (`Export Ledger`, `Open Positions`).
- Metric labels: sentence case (`Daily loss remaining`).
- Statuses and symbols: ALL CAPS (`HOLD`, `TARGET REACHED`, `INJUSDT`).
- Secondary copy no dimmer than `text-zinc-500`; `zinc-600` is retired for text.
- Account admin (Rename, Export Ledger, Delete Account) lives behind a `•••` menu.

## Deliberate simplifications (ponytail)

- All six intervals are subscribed directly rather than deriving 5m..1d from 1m. One code
  path, exchange-authoritative candles, at the cost of 140 streams. Past ~150 symbols the
  1024-stream cap forces derivation.
- The volume-multiple headline uses base-asset volume on both sides (last 24 hourly bars
  vs mean of 30 closed UTC days).
- REST depth uses 1000 levels (weight 50). On ETH that reaches about 0.5% from mid, on
  BTC less; the panel shows how far the snapshot reaches and dims bands it cannot fill.
  The full 5000-level book costs weight 250 per call, too much to poll.
- Every interval keeps RING_SIZE bars, so 1d history is 1500 days (about four years),
  more than the two years of 1h. It costs two REST calls per symbol.
- Client queues drop the oldest frame rather than coalescing same-key frames. Coalescing
  is the upgrade if a client ever needs more than ~200 frames of slack.
- Base rates recompute on every `/api/context` call (a few tens of ms in pure Python over
  17,500 bars). Cache them if the panel is ever polled faster than every few seconds.
