"""Shape engine: rolling math matches the naive formulas, and synthetic series get the labels
a human would give them."""
import math
import random
import statistics

import pytest

from app.models import Candle
from app.shape import label_series, rolling_extreme, rolling_regression, rolling_sum, shape_report, variance_ratio

H = 3_600_000
T0 = 1_700_006_400_000


def test_rolling_regression_matches_statistics_module():
    random.seed(1)
    y = [math.log(100 + i * 0.3 + random.gauss(0, 2)) for i in range(120)]
    slope, r2 = rolling_regression(y, 30)
    for i in (29, 60, 119):
        window = y[i - 29:i + 1]
        b, _ = statistics.linear_regression(range(30), window)
        assert slope[i] == pytest.approx(b, rel=1e-9)
        assert r2[i] == pytest.approx(statistics.correlation(range(30), window) ** 2, rel=1e-9)
    assert slope[28] is None


def test_rolling_sum_and_extreme():
    xs = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0]
    assert rolling_sum(xs, 3)[2:] == [8.0, 6.0, 10.0, 15.0, 16.0]
    vals, idxs = rolling_extreme(xs, 3, True)
    assert vals[2:] == [4.0, 4.0, 5.0, 9.0, 9.0] and idxs[5] == 5
    vals, idxs = rolling_extreme(xs, 3, False)
    assert vals[2:] == [1.0, 1.0, 1.0, 1.0, 2.0]


def test_variance_ratio_distinguishes_trend_from_reversion():
    random.seed(2)
    noise = [random.gauss(0, 1) for _ in range(600)]
    momentum = [noise[i] + 0.7 * noise[i - 1] for i in range(1, 600)]       # positively autocorrelated
    reverting = [noise[i] - 0.7 * noise[i - 1] for i in range(1, 600)]      # negatively autocorrelated
    assert variance_ratio(momentum, 4) > 1.2
    assert variance_ratio(reverting, 4) < 0.8
    assert 0.8 < variance_ratio(noise, 4) < 1.2


def series(closes, vols=None):
    vols = vols or [10.0] * len(closes)
    return [Candle("X", "1h", T0 + i * H, c, c * 1.002, c * 0.998, c, v, True) for i, (c, v) in enumerate(zip(closes, vols))]


def base(n=900):
    random.seed(3)
    out, p = [], 100.0
    for _ in range(n):
        p *= 1 + random.gauss(0, 0.002)
        out.append(p)
    return out


def test_labels_for_synthetic_shapes():
    # Blow-off: +25% in the last 24 bars on 4x volume.
    closes = base()
    p = closes[-1]
    closes += [p * (1 + 0.25 * (k + 1) / 24) for k in range(24)]
    vols = [10.0] * 900 + [40.0] * 24
    labels, m = label_series(series(closes, vols))
    assert labels[-1] == "blowoff_up" and m["move24"] > 2 and m["volumeX"] > 2.5

    # Bull flag: +20% impulse over two days, then 24 flat bars.
    closes = base()
    p = closes[-1]
    closes += [p * (1 + 0.20 * (k + 1) / 48) for k in range(48)] + [p * 1.2 + (0.01 * (k % 2)) for k in range(24)]
    labels, m = label_series(series(closes))
    assert labels[-1] == "flag_up" and m["range24"] < 0.5

    # Dead cat: -20% drop, then bounce 40% of the way back.
    closes = base()
    p = closes[-1]
    drop = [p * (1 - 0.20 * (k + 1) / 36) for k in range(36)]
    low = drop[-1]
    bounce = [low + (p - low) * 0.4 * (k + 1) / 30 for k in range(30)]
    labels, m = label_series(series(closes + drop + bounce))
    assert labels[-1] == "dead_cat" and m["fellFirst"] and 0.2 <= m["retrace"] <= 0.6

    # Clean uptrend: +0.3% per bar with tiny noise for three days.
    closes = base()
    p = closes[-1]
    random.seed(4)
    closes += [p * (1.003 ** (k + 1)) * (1 + random.gauss(0, 0.0005)) for k in range(80)]
    labels, m = label_series(series(closes))
    assert labels[-1] == "trend_up" and m["r2_72"] > 0.6 and m["efficiency72"] > 0.45


def test_shape_report_carries_base_rates_for_the_current_label():
    random.seed(5)
    closes = base(2000)
    rep = shape_report(series(closes))
    assert rep is not None and rep["label"] in ("range", "chop", "squeeze", "trend_up", "trend_down", "dead_cat", "v_reversal")
    assert set(rep["horizons"]) == {"1h", "4h", "24h"}
    assert rep["nBars"] >= rep["nEpisodes"] >= 1
