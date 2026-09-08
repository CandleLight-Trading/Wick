"""Assembles the Context panel payload for one symbol. Numbers and distributions only."""
from .config import ACCOUNT_SIZE_USD, CONDITION_INTERVAL, HORIZONS_BARS, MIN_SAMPLE, ROUND_TRIP_COST, SLIPPAGE_NOTIONALS
from .flow import slippage_table, taker_ratio, taker_ratio_series_mean
from .futures import summarize_history
from .indicators import (CONDITION_NAMES, atr, base_rates, book_bands, correlation, evaluate_conditions,
                         histogram, log_returns, pct_distance, realized_vol, rsi, sma)
from .store import Store
from .timeutil import ms_to_s, now_ms

CAVEATS = (
    "Historical base rates on limited local data. Not predictive. In-sample, no slippage, "
    "survivorship-affected (tracked symbols were chosen for being liquid today). Episodes cluster "
    "in volatile regimes, so the Wilson interval understates true uncertainty. "
    f"Net figures deduct a {ROUND_TRIP_COST * 100:.2f}% round-trip taker fee."
)

BTC = "BTCUSDT"


async def build_context(store: Store, symbol: str, tracked_count: int) -> dict:
    h1 = await store.closed_history(symbol, CONDITION_INTERVAL)
    d1 = await store.closed_history(symbol, "1d")
    if len(h1) < 30:
        raise ValueError(f"only {len(h1)} closed 1h candles stored for {symbol}; wait for backfill")

    closes = [c.close for c in h1]
    ticker = store.tickers.get(symbol)
    price = ticker.last if ticker else closes[-1]

    # Volume vs 30-day average: rolling last 24 hourly bars vs mean of the last 30 closed UTC days.
    last24 = sum(c.volume for c in h1[-24:])
    days = d1[-30:]
    avg_day = sum(c.volume for c in days) / len(days) if days else None
    volume = {"last24h": last24, "avg30d": avg_day, "days": len(days),
              "multiple": last24 / avg_day if avg_day else None}

    # Volatility: annualised realised vol from 30 days of hourly returns; ATR(14) on daily bars
    # so "today's move" can be compared with a typical day's range.
    atr_d = atr(d1)[-1] if len(d1) >= 14 else None
    today_move = pct_distance(price, d1[-1].close) if d1 else None
    volatility = {
        "realized30dAnnualized": realized_vol(closes[-720:], 24 * 365),
        "realized7dAnnualized": realized_vol(closes[-168:], 24 * 365),
        "atr14Daily": atr_d, "atr14DailyPct": atr_d / price * 100 if atr_d else None,
        "todayMovePct": today_move,
        "todayMoveInAtr": abs(today_move) / (atr_d / price * 100) if atr_d and today_move is not None else None,
        "atr14Hourly": atr(h1[-100:])[-1],
    }

    mas = {}
    for n in (20, 50, 200):
        ma = sma(closes, n)[-1] if len(closes) >= n else None
        mas[str(n)] = {"value": ma, "distancePct": pct_distance(price, ma)}

    r = rsi(closes)
    r_hist = [v for v in r if v is not None]
    rsi_block = {"value": r[-1], "histogram": histogram(r_hist, r[-1])}

    depth = store.depth_rest.get(symbol) or store.depth_live.get(symbol)
    book = book_bands(depth) if depth else None
    if book:
        book["source"] = "rest" if symbol in store.depth_rest else "ws-top20"
        book["ageS"] = (now_ms() - depth.ts) / 1000

    # Correlation to BTC: 30 days of daily log returns, aligned on open_time.
    corr = None
    if symbol != BTC and len(d1) >= 8:
        btc_d = await store.closed_history(BTC, "1d", limit=40)
        btc_close = {c.open_time: c.close for c in btc_d}
        pairs = [(c.close, btc_close[c.open_time]) for c in d1[-31:] if c.open_time in btc_close]
        if len(pairs) >= 8:
            corr = correlation(log_returns([p[0] for p in pairs]), log_returns([p[1] for p in pairs]))
    elif symbol == BTC:
        corr = 1.0

    conds = evaluate_conditions(h1)

    # Phase 4: order flow, implied vol, slippage at your sizes.
    flow = {"takerBuyRatio24": taker_ratio(h1, 24), "takerBuyRatio7d": taker_ratio(h1, 168),
            "takerBuyRatio30dMean": taker_ratio_series_mean(h1, 720)}
    base_asset = symbol.replace("USDT", "")
    implied = store.dvol.get(base_asset)
    rv30 = volatility["realized30dAnnualized"]
    vol_block = None
    if implied is not None:
        vol_block = {"impliedVol30dPct": implied, "realizedVol30dPct": rv30 * 100 if rv30 else None,
                     "variancePremiumPct": implied - rv30 * 100 if rv30 else None, "source": "Deribit DVOL"}
    depth_for_slip = store.depth_rest.get(symbol) or store.depth_scan.get(symbol)
    slippage = slippage_table(depth_for_slip, sorted(set(SLIPPAGE_NOTIONALS + [ACCOUNT_SIZE_USD]))) if depth_for_slip else None

    # Phase 2: perpetual-futures positioning and cross-exchange divergence.
    f = store.funding.get(symbol)
    futures = None
    if f:
        hist = store.funding_history.get(symbol, [])
        futures = {
            **f.to_wire(),
            "basisPct": pct_distance(f.mark_price, price) if f.mark_price else None,
            "fundingAnnualizedPct": f.funding_rate * 3 * 365 * 100 if f.funding_rate is not None else None,
            "openInterestNotional": f.open_interest * f.mark_price if f.open_interest and f.mark_price else None,
            "history": [{"time": ms_to_s(p.time), "rate": p.rate} for p in hist],
            "historySummary": summarize_history(hist),
        }
    cross = {}
    for name, tickers in store.other_tickers.items():
        t = tickers.get(symbol)
        if t:
            cross[name] = {**t.to_wire(), "divergenceBps": (t.last / price - 1) * 10_000 if price else None,
                           "ageS": (now_ms() - t.event_time) / 1000}

    return {
        "flow": flow, "impliedVol": vol_block, "slippage": slippage, "accountSize": ACCOUNT_SIZE_USD,
        "futures": futures, "futuresStatus": store.futures_status, "crossExchange": cross,
        "symbol": symbol, "price": price, "asOf": ms_to_s(now_ms()),
        "history": {"bars1h": len(h1), "from": ms_to_s(h1[0].open_time), "to": ms_to_s(h1[-1].open_time), "bars1d": len(d1)},
        "volume": volume, "volatility": volatility, "ma": mas, "rsi": rsi_block, "book": book,
        "correlationBtc30d": corr,
        "conditionsTrue": [n for n, f in conds.items() if f and f[-1]],
        "baseRates": base_rates(h1, conds),
        "testsEvaluated": {"total": tracked_count * len(CONDITION_NAMES) * len(HORIZONS_BARS),
                           "symbols": tracked_count, "conditions": len(CONDITION_NAMES), "horizons": len(HORIZONS_BARS)},
        "minSample": MIN_SAMPLE, "roundTripCost": ROUND_TRIP_COST, "caveats": CAVEATS,
    }
