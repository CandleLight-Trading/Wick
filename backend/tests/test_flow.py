import math
import random

import pytest

from app.flow import beta, breadth, daily_tracker, ewma_vol, log_returns_aligned, slippage_table, taker_ratio, walk_book
from app.indicators import forward_stats
from app.models import Candle, Depth

H = 3_600_000
T0 = 1_700_006_400_000


def test_taker_ratio_uses_only_bars_with_data():
    cs = [Candle("X", "1h", T0 + i * H, 1, 1, 1, 1, 10.0, True, taker_buy=None) for i in range(3)]
    cs += [Candle("X", "1h", T0 + i * H, 1, 1, 1, 1, 10.0, True, taker_buy=7.0) for i in range(3, 6)]
    assert taker_ratio(cs, 6) == pytest.approx(0.7)
    assert taker_ratio(cs[:3], 3) is None


def test_walk_book_and_slippage():
    asks = [(100.0, 1.0), (101.0, 2.0), (103.0, 10.0)]
    bids = [(99.0, 1.0), (98.0, 2.0), (95.0, 10.0)]
    small = walk_book(asks, 50.0)                         # fits in the first level
    assert small["avgPrice"] == 100.0 and small["filledFraction"] == 1.0
    big = walk_book(asks, 300.0)                          # 100 + 202 = 302 > 300: partly in level 2
    assert 100.0 < big["avgPrice"] < 101.0 and big["filledFraction"] == pytest.approx(1.0)
    huge = walk_book(asks, 10_000.0)
    assert huge["filledFraction"] < 1.0                   # book too thin, reported not hidden
    t = slippage_table(Depth("X", bids, asks, 0), [50.0, 300.0])
    assert t["mid"] == 99.5
    assert t["rows"][0]["buyBps"] == pytest.approx((100 / 99.5 - 1) * 1e4)
    assert t["rows"][1]["sellBps"] > t["rows"][0]["sellBps"]


def test_beta_and_aligned_returns():
    random.seed(7)
    btc = [100.0]
    for _ in range(60):
        btc.append(btc[-1] * (1 + random.gauss(0, 0.02)))
    alt = [b ** 1.5 / 10 for b in btc]                    # log-return beta of exactly 1.5
    a = [Candle("A", "1d", T0 + i * 86_400_000, p, p, p, p, 1, True) for i, p in enumerate(alt)]
    b = [Candle("B", "1d", T0 + i * 86_400_000, p, p, p, p, 1, True) for i, p in enumerate(btc)]
    ra, rb = log_returns_aligned(a, b[5:])                # misaligned starts are handled
    assert len(ra) == len(rb) == 55
    assert beta(ra, rb) == pytest.approx(1.5, rel=1e-6)


def test_ewma_vol_tracks_recent_regime():
    calm = [0.001] * 200
    assert ewma_vol(calm) == pytest.approx(0.001, rel=1e-3)
    shocked = calm + [0.05] * 30
    assert ewma_vol(shocked) > 0.03                       # forecast moves toward the new regime


def test_breadth_and_daily_tracker():
    rows = [{"ma20Pct": 1, "ma50Pct": -1, "ma200Pct": 2, "change24hPct": 3}, {"ma20Pct": -1, "ma50Pct": -2, "ma200Pct": 1, "change24hPct": -1}]
    b = breadth(rows)
    assert b["above200"] == 1.0 and b["above50"] == 0.0 and b["up24h"] == 0.5
    t = daily_tracker(closed_today_pnl=-250.0, open_unrealized=-50.0, equity_start=10_000, daily_limit_pct=4.0,
                      max_dd_pct=8.0, peak_equity=10_400, equity_now=9_700)
    assert t["todayPnlPct"] == pytest.approx(-3.0) and t["dailyBudgetUsedPct"] == pytest.approx(75.0)
    assert t["drawdownPct"] == pytest.approx(700 / 10_400 * 100) and t["breached"] is False
    assert daily_tracker(-450, 0, 10_000, 4.0, 8.0, 10_000, 9_550)["breached"] is True


def test_walk_forward_halves_in_forward_stats():
    closes = [100.0 * (1.001 ** i) for i in range(400)]   # steady rise: every long wins gross
    idxs = list(range(0, 380, 5))
    s = forward_stats(closes, idxs, 4, cost=0.0)
    assert s["firstHalf"]["n"] == 40 and s["secondHalf"]["n"] == 36
    assert s["firstHalf"]["hitRateNet"] == 1.0 and s["secondHalf"]["hitRateNet"] == 1.0
    assert math.isclose(s["hitRateNet"], 1.0)
