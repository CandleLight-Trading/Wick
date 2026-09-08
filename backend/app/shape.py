"""The shape of recent price action, in numbers. Pure, stdlib only, O(n) over the history.

Chart patterns people draw by eye (flags, dead-cat bounces, blow-off tops) are really
statements about a few measurable quantities:

  slope, R^2      linear regression of log price over a window: direction and how
                  straight the path was (R^2 near 1 = a clean line, near 0 = noise)
  efficiency      Kaufman's ratio: |net move| / sum |bar moves|. 1 = straight line,
                  0 = went nowhere despite lots of motion
  variance ratio  Var(q-bar returns) / (q * Var(1-bar returns)). > 1 returns trend
                  (momentum), < 1 they mean-revert, = 1 random walk (Lo-MacKinlay)
  bandwidth       20-bar std / mean, vs its own 30-day average. Low = squeeze
  range           high-low of the last 24 bars in units of a typical day
  retrace         where price sits between the 3-day low and high, and which came first
  volume climax   last 24h volume vs the 30-day hourly average

Everything is scaled by `unit`, one typical day's range for this coin (hourly ATR(14)
times sqrt(24)), so the same thresholds mean the same thing for BTC and for a memecoin.

Each bar gets one label. The current label's history on this symbol is then run through
the same onset / forward-return machinery as the base-rate strip, so "flag after an
impulse" comes with "n=41 episodes on this coin, 54% [39-68%] positive at +24h" and not
with a textbook promise.

Rolling statistics use prefix sums and an incremental Sxy so the whole 17,500-bar
history costs one pass per statistic.
"""
import math
import statistics
from collections import deque

from .config import HORIZONS_BARS
from .indicators import atr, forward_stats, onsets, rsi
from .models import Candle

SQRT24 = math.sqrt(24)


# ---- rolling primitives (all O(n)) ------------------------------------------------
def rolling_sum(xs: list[float], w: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, v in enumerate(xs):
        s += v
        if i >= w:
            s -= xs[i - w]
        if i >= w - 1:
            out[i] = s
    return out


def rolling_regression(y: list[float], w: int) -> tuple[list[float | None], list[float | None]]:
    """Slope per bar (units of y per bar) and R^2 of y against bar index over the trailing w bars.
    Sxy is updated incrementally: shifting the window left by one turns every x into x-1, so
    Sxy_new = Sxy_old + w*y_new - Sy_new."""
    n = len(y)
    slope: list[float | None] = [None] * n
    r2: list[float | None] = [None] * n
    if n < w:
        return slope, r2
    sx = w * (w - 1) / 2
    sxx = (w - 1) * w * (2 * w - 1) / 6
    var_x = w * sxx - sx * sx
    sy = sum(y[:w])
    syy = sum(v * v for v in y[:w])
    sxy = sum(k * y[k] for k in range(w))
    for i in range(w - 1, n):
        if i >= w:
            old, new = y[i - w], y[i]
            sy += new - old
            syy += new * new - old * old
            sxy += w * new - sy
        var_y = w * syy - sy * sy
        b = (w * sxy - sx * sy) / var_x
        slope[i] = b
        r2[i] = (b * b * var_x / var_y) if var_y > 1e-18 else 0.0
    return slope, r2


def rolling_extreme(xs: list[float], w: int, want_max: bool) -> tuple[list[float | None], list[int | None]]:
    """Sliding-window max (or min) and the index where it occurred, via a monotonic deque."""
    vals: list[float | None] = [None] * len(xs)
    idxs: list[int | None] = [None] * len(xs)
    dq: deque[int] = deque()
    for i, v in enumerate(xs):
        while dq and ((xs[dq[-1]] <= v) if want_max else (xs[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            idxs[i] = dq[0]
            vals[i] = xs[dq[0]]
    return vals, idxs


def variance_ratio(returns: list[float], q: int) -> float | None:
    """Lo-MacKinlay variance ratio on a return series. Uses overlapping q-sums."""
    if len(returns) < q * 4:
        return None
    v1 = statistics.pvariance(returns)
    if v1 <= 0:
        return None
    qs = [sum(returns[i:i + q]) for i in range(len(returns) - q + 1)]
    return statistics.pvariance(qs) / (q * v1)


def autocorr(returns: list[float], lag: int = 1) -> float | None:
    if len(returns) <= lag + 2:
        return None
    try:
        return statistics.correlation(returns[:-lag], returns[lag:])
    except statistics.StatisticsError:
        return None


# ---- labels ------------------------------------------------------------------------
SHAPES = {
    "blowoff_up": ("Blow-off / parabolic", "A vertical move on climactic volume with RSI pinned high. Textbooks call this exhaustion; the crowd is all in."),
    "capitulation": ("Capitulation", "A vertical drop on climactic volume with RSI pinned low. Forced selling; often followed by a bounce, sometimes by more."),
    "flag_up": ("Impulse then compression (bull flag)", "A big up-move, then a tight range on fading volume. Textbook says continuation; check the base rate."),
    "flag_down": ("Impulse then compression (bear flag)", "A big down-move, then a tight range on fading volume. Textbook says continuation lower."),
    "dead_cat": ("Drop then partial bounce", "Fell hard, retraced 20-60% of it. Could be a dead-cat bounce or the start of a recovery; the volume on the bounce decides."),
    "v_reversal": ("V-reversal", "Fell hard, then recovered most of the drop. Sellers were absorbed fast."),
    "trend_up": ("Clean uptrend (staircase)", "Path is straight and efficient. Momentum regime; pullbacks have been bought."),
    "trend_down": ("Clean downtrend", "Path is straight and efficient to the downside. Rallies have been sold."),
    "squeeze": ("Volatility squeeze", "Bandwidth is far below its own average. Compression tends to resolve with a big move; direction is not implied."),
    "range": ("Range / mean-reverting", "Lots of motion, no net progress. Edges of the range have held; breakouts have failed."),
    "chop": ("No clear shape", "None of the geometric conditions hold. Noise."),
}


def label_series(h1: list[Candle]) -> tuple[list[str | None], dict]:
    """One label per bar. Returns the series and the current-bar metrics used to pick it."""
    n = len(h1)
    closes = [c.close for c in h1]
    highs = [c.high for c in h1]
    lows = [c.low for c in h1]
    vols = [c.volume for c in h1]
    labels: list[str | None] = [None] * n
    if n < 200:
        return labels, {}

    logp = [math.log(c) for c in closes]
    slope24, r2_24 = rolling_regression(logp, 24)
    slope72, r2_72 = rolling_regression(logp, 72)
    atr_h = atr(h1, 14)
    absd = [0.0] + [abs(closes[i] - closes[i - 1]) for i in range(1, n)]
    path72 = rolling_sum(absd, 72)
    hi72, hi72_i = rolling_extreme(highs, 72, True)
    lo72, lo72_i = rolling_extreme(lows, 72, False)
    hi24, _ = rolling_extreme(highs, 24, True)
    lo24, _ = rolling_extreme(lows, 24, False)
    vol24 = rolling_sum(vols, 24)
    vol720 = rolling_sum(vols, 720)
    r = rsi(closes)
    # bandwidth: std/mean over 20 bars via prefix sums, then its 720-bar mean
    s20 = rolling_sum(closes, 20)
    ss20 = rolling_sum([c * c for c in closes], 20)
    bw: list[float] = [0.0] * n
    for i in range(n):
        if s20[i] is not None:
            mean = s20[i] / 20
            var = max(ss20[i] / 20 - mean * mean, 0.0)
            bw[i] = math.sqrt(var) / mean if mean else 0.0
    bw720 = rolling_sum(bw, 720)

    metrics = {}
    for i in range(200, n):
        if atr_h[i] is None or path72[i] is None:
            continue
        unit = atr_h[i] * SQRT24                       # one typical day's range, in price
        if unit <= 0:
            continue
        m24 = (closes[i] - closes[i - 24]) / unit
        m72 = (closes[i] - closes[i - 72]) / unit
        er72 = abs(closes[i] - closes[i - 72]) / path72[i] if path72[i] else 0.0
        rng24 = (hi24[i] - lo24[i]) / unit
        drop = (hi72[i] - lo72[i]) / unit
        span = hi72[i] - lo72[i]
        retrace = (closes[i] - lo72[i]) / span if span > 0 else 0.5
        fell_first = hi72_i[i] < lo72_i[i]
        volx = (vol24[i] / 24) / (vol720[i] / 720) if vol720[i] else None
        squeeze = bw720[i] is not None and bw720[i] > 0 and bw[i] < 0.5 * (bw720[i] / 720)
        rs = r[i]

        # Thresholds sit at percentiles of the real hourly distributions (BTC, DOT, SOL, two
        # years): |m24| p95 = 1.2, volX p95 = 2.0, |m72| p75 = 1.3, range24 p10 = 0.75,
        # ER72 p90 = 0.25, R2_72 p75 = 0.7, |m72| p25 = 0.4, ER72 p25 = 0.05.
        if m24 >= 1.2 and rs is not None and rs >= 75 and volx is not None and volx >= 2.0 and slope24[i] > 0:
            lab = "blowoff_up"
        elif m24 <= -1.2 and rs is not None and rs <= 25 and volx is not None and volx >= 2.0:
            lab = "capitulation"
        elif m72 >= 1.3 and rng24 <= 0.75 and abs(m24) <= 0.3:
            lab = "flag_up"
        elif m72 <= -1.3 and rng24 <= 0.75 and abs(m24) <= 0.3:
            lab = "flag_down"
        elif fell_first and drop >= 1.8 and retrace >= 0.8:
            lab = "v_reversal"
        elif fell_first and drop >= 1.8 and 0.2 <= retrace <= 0.6:
            lab = "dead_cat"
        elif er72 >= 0.25 and r2_72[i] >= 0.7 and slope72[i] > 0:
            lab = "trend_up"
        elif er72 >= 0.25 and r2_72[i] >= 0.7 and slope72[i] < 0:
            lab = "trend_down"
        elif squeeze:
            lab = "squeeze"
        elif abs(m72) < 0.4 and er72 < 0.05:
            lab = "range"
        else:
            lab = "chop"
        labels[i] = lab
        if i == n - 1:
            rets = [logp[k] - logp[k - 1] for k in range(n - 168, n)]
            metrics = {
                "unitPct": unit / closes[i] * 100, "move24": m24, "move72": m72,
                "slope24PctPerDay": (math.exp(slope24[i] * 24) - 1) * 100, "r2_24": r2_24[i],
                "slope72PctPerDay": (math.exp(slope72[i] * 24) - 1) * 100, "r2_72": r2_72[i],
                "efficiency72": er72, "range24": rng24, "drop72": drop, "retrace": retrace, "fellFirst": fell_first,
                "volumeX": volx, "bandwidth": bw[i], "bandwidthVsAvg": bw[i] / (bw720[i] / 720) if bw720[i] else None,
                "varianceRatio4": variance_ratio(rets, 4), "varianceRatio24": variance_ratio(rets, 24),
                "autocorr1": autocorr(rets), "rsi": rs,
                "drawdownFrom7dHighPct": (closes[i] / max(highs[i - 168:i + 1]) - 1) * 100,
            }
    return labels, metrics


def shape_report(h1: list[Candle]) -> dict | None:
    """Current shape, its metrics, and how this shape played out on this symbol before."""
    labels, metrics = label_series(h1)
    if not labels or labels[-1] is None:
        return None
    current = labels[-1]
    flags = [None if l is None else (l == current) for l in labels]
    idxs = onsets(flags)
    closes = [c.close for c in h1]
    name, description = SHAPES[current]
    return {
        "label": current, "name": name, "description": description, "metrics": metrics,
        "mfe48h": {side: median_favorable_excursion(h1, labels, current, 48, side) for side in ("long", "short")},
        "nEpisodes": len(idxs), "nBars": sum(1 for f in flags if f),
        "horizons": {lbl: forward_stats(closes, idxs, h) for lbl, h in HORIZONS_BARS.items()},
        "history": [{"label": l, "count": c} for l, c in _counts(labels).items()],
    }


def median_favorable_excursion(h1: list[Candle], labels: list[str | None], label: str, horizon_bars: int, side: str) -> float | None:
    """Median of the best price reached (in the trade direction) within `horizon_bars` after
    each onset of `label`, as a fraction of the onset close. The planner's structural target."""
    flags = [None if l is None else (l == label) for l in labels]
    idxs = [i for i in onsets(flags) if i + horizon_bars < len(h1)]
    if len(idxs) < 5:
        return None
    exc = []
    for i in idxs:
        window = h1[i + 1:i + 1 + horizon_bars]
        base = h1[i].close
        best = max(c.high for c in window) if side == "long" else min(c.low for c in window)
        exc.append((best / base - 1) if side == "long" else (1 - best / base))
    return statistics.median(exc)


def _counts(labels):
    out: dict[str, int] = {}
    for l in labels:
        if l:
            out[l] = out.get(l, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
