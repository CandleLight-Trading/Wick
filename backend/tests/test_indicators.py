from app.indicators import forward_stats, onsets, rsi, sma, wilson


def test_sma():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_rsi_extremes():
    up = list(range(1, 40))
    assert rsi(up)[-1] == 100.0
    down = list(range(40, 1, -1))
    assert rsi(down)[-1] == 0.0


def test_onsets_collapse_consecutive_true_bars():
    flags = [None, False, True, True, True, False, True, False, False, True]
    assert onsets(flags) == [2, 6, 9]


def test_wilson_is_narrower_with_more_data():
    lo1, hi1 = wilson(6, 10)
    lo2, hi2 = wilson(600, 1000)
    assert hi1 - lo1 > hi2 - lo2
    assert lo2 < 0.6 < hi2


def test_forward_stats_applies_cost_and_flags_overlap():
    closes = [100.0] * 50
    for i in range(1, 50):
        closes[i] = closes[i - 1] * 1.001             # +0.1% per bar
    idxs = list(range(0, 40, 2))                       # onset every 2 bars
    s = forward_stats(closes, idxs, horizon_bars=1, cost=0.002)
    assert s["n"] == 20
    assert s["hitRateGross"] == 1.0
    assert s["hitRateNet"] == 0.0                      # +0.1% gross is -0.1% after 0.2% cost
    assert s["overlapping"] is False                   # 1-bar horizon, 2-bar spacing
    assert forward_stats(closes, idxs, horizon_bars=4, cost=0.002)["overlapping"] is True
    assert s["underpowered"] is True                   # n=20 < 30
