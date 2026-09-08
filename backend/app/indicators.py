"""Pure indicator math over plain lists. stdlib only, so every formula is readable.

Nothing here emits a verdict. Functions return numbers and distributions; the
context panel shows them with sample sizes and intervals so you interpret them.
"""
import math
import statistics

from .config import HORIZONS_BARS, MIN_SAMPLE, ROUND_TRIP_COST
from .models import Candle, Depth


# ---- moving averages, RSI, ATR ------------------------------------------------
def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI: seed with simple averages, then smooth with alpha = 1/n."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    return 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles: list[Candle], n: int = 14) -> list[float | None]:
    """Wilder ATR over true range = max(h-l, |h-prev_close|, |l-prev_close|)."""
    if not candles:
        return []
    trs = [candles[0].high - candles[0].low]
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    out: list[float | None] = [None] * len(trs)
    if len(trs) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ---- returns, volatility, correlation ---------------------------------------
def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def realized_vol(closes: list[float], periods_per_year: float) -> float | None:
    """Annualised standard deviation of log returns, as a fraction (0.65 = 65%)."""
    r = log_returns(closes)
    if len(r) < 2:
        return None
    return statistics.pstdev(r) * math.sqrt(periods_per_year)


def correlation(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(a) != len(b):
        return None
    try:
        return statistics.correlation(a, b)
    except statistics.StatisticsError:
        return None


def pct_distance(price: float, ref: float | None) -> float | None:
    return None if not ref else (price / ref - 1.0) * 100.0


def histogram(values: list[float], current: float | None, lo=0.0, hi=100.0, bins=10) -> dict:
    """Where does `current` sit in this symbol's own distribution of `values`?"""
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(int((v - lo) // width), bins - 1)] += 1
    cur_bin = None if current is None else min(int((current - lo) // width), bins - 1)
    pct = None if current is None or not values else 100.0 * sum(1 for v in values if v <= current) / len(values)
    return {
        "bins": [{"lo": lo + i * width, "hi": lo + (i + 1) * width, "count": c} for i, c in enumerate(counts)],
        "currentBin": cur_bin, "percentile": pct, "n": len(values),
    }


# ---- order book -------------------------------------------------------------
def book_bands(depth: Depth, widths=(0.005, 0.01)) -> dict | None:
    """Bid vs ask notional within +-width of mid, plus spread in basis points.
    `coveragePct` says how far from mid the snapshot actually reaches: a 1% band on a
    book that only covers 0.3% is meaningless and the UI says so."""
    if not depth.bids or not depth.asks:
        return None
    best_bid, best_ask = depth.bids[0][0], depth.asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    bands = []
    for w in widths:
        bid_n = sum(p * q for p, q in depth.bids if p >= mid * (1 - w))
        ask_n = sum(p * q for p, q in depth.asks if p <= mid * (1 + w))
        total = bid_n + ask_n
        bands.append({"widthPct": w * 100, "bidNotional": bid_n, "askNotional": ask_n,
                      "imbalance": (bid_n - ask_n) / total if total else None})
    coverage = min((mid - depth.bids[-1][0]) / mid, (depth.asks[-1][0] - mid) / mid) * 100
    return {"mid": mid, "spreadBps": (best_ask - best_bid) / mid * 10_000, "bands": bands,
            "coveragePct": coverage, "levels": len(depth.bids)}


# ---- conditions and base rates ----------------------------------------------
# Every condition is a boolean series over closed 1h candles. None means "not enough
# history yet". The same definitions feed both the in-sample base-rate strip and the
# live condition log, so the two are directly comparable.
CONDITION_NAMES = [
    "RSI(14) < 30",
    "RSI(14) > 70",
    "Volume > 2x 30d avg",
    "Price below 200MA",
    "Price above 200MA",
    "Price below 50MA",
]


def evaluate_conditions(candles: list[Candle]) -> dict[str, list[bool | None]]:
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]
    r = rsi(closes)
    s50, s200 = sma(closes, 50), sma(closes, 200)
    v_avg = sma(vols, 24 * 30)     # 30 days of hourly bars
    n = len(candles)

    def cmp(series, f):
        return [None if series[i] is None else f(i) for i in range(n)]

    return {
        "RSI(14) < 30": cmp(r, lambda i: r[i] < 30),
        "RSI(14) > 70": cmp(r, lambda i: r[i] > 70),
        "Volume > 2x 30d avg": cmp(v_avg, lambda i: vols[i] > 2 * v_avg[i]),
        "Price below 200MA": cmp(s200, lambda i: closes[i] < s200[i]),
        "Price above 200MA": cmp(s200, lambda i: closes[i] > s200[i]),
        "Price below 50MA": cmp(s50, lambda i: closes[i] < s50[i]),
    }


def onsets(flags: list[bool | None]) -> list[int]:
    """Indices where a condition turns True after being False. Consecutive True bars
    collapse into one episode, so 'RSI < 30 for six hours' counts once, not six times."""
    return [i for i in range(1, len(flags)) if flags[i] and flags[i - 1] is False]


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score interval for a proportion. Assumes independent trials, which
    clustered market episodes are not, so read it as a floor on the uncertainty."""
    if n == 0:
        return None, None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def forward_stats(closes: list[float], idxs: list[int], horizon_bars: int, cost: float = ROUND_TRIP_COST) -> dict:
    """Distribution of forward returns `horizon_bars` after each onset index.
    Net figures subtract the round-trip taker cost (0.20%) as a flat deduction; the exact
    (1+r)(1-f)^2 - 1 differs from r - 2f by under a basis point at these sizes."""
    gross = [closes[i + horizon_bars] / closes[i] - 1.0 for i in idxs if i + horizon_bars < len(closes)]
    n = len(gross)
    gaps = [b - a for a, b in zip(idxs, idxs[1:])]
    median_gap = statistics.median(gaps) if gaps else None
    base = {"horizonBars": horizon_bars, "n": n, "underpowered": n < MIN_SAMPLE,
            "overlapping": median_gap is not None and median_gap < horizon_bars, "medianGapBars": median_gap}
    if n == 0:
        return base
    net = [r - cost for r in gross]
    hits_net = sum(1 for r in net if r > 0)
    lo, hi = wilson(hits_net, n)
    q = statistics.quantiles(net, n=4) if n >= 2 else [net[0]] * 3
    # Walk-forward check: episodes in the first half of history vs the second half. If the
    # second-half rate is much worse than the first, the "edge" was fitted, not found.
    split = len(closes) // 2
    valid = [i for i in idxs if i + horizon_bars < len(closes)]
    halves = {}
    for name, sel in (("firstHalf", [i for i in valid if i < split]), ("secondHalf", [i for i in valid if i >= split])):
        k = sum(1 for i in sel if closes[i + horizon_bars] / closes[i] - 1 - cost > 0)
        h_lo, h_hi = wilson(k, len(sel))
        halves[name] = {"n": len(sel), "hitRateNet": k / len(sel) if sel else None, "ciLo": h_lo, "ciHi": h_hi,
                        "underpowered": len(sel) < MIN_SAMPLE}
    return {**base,
            "hitRateNet": hits_net / n, "ciLo": lo, "ciHi": hi,
            "hitRateGross": sum(1 for r in gross if r > 0) / n,
            "medianNet": statistics.median(net), "p25Net": q[0], "p75Net": q[2],
            "medianGross": statistics.median(gross), **halves}


def base_rates(candles: list[Candle], conditions: dict[str, list[bool | None]]) -> list[dict]:
    """For each condition that is true on the latest bar, its historical forward-return
    distribution at every horizon. Only currently-true conditions are shown because the
    question the panel answers is 'what happened after moments like this one?'."""
    closes = [c.close for c in candles]
    out = []
    for name, flags in conditions.items():
        if not flags or not flags[-1]:
            continue
        idxs = onsets(flags)
        out.append({
            "condition": name,
            "nBars": sum(1 for f in flags if f),
            "nEpisodes": len(idxs),
            "horizons": {label: forward_stats(closes, idxs, h) for label, h in HORIZONS_BARS.items()},
        })
    return out
