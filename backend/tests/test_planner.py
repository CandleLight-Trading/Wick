import pytest

from app.models import Candle
from app.planner import MIN_RR, build_plan, size_for, structural_targets, swing_stop

H = 3_600_000
T0 = 1_700_006_400_000


def bars(closes, wick=0.5):
    return [Candle("X", "1h", T0 + i * H, c, c + wick, c - wick, c, 1.0, True) for i, c in enumerate(closes)]


def test_swing_stop_uses_recent_extreme_and_buffer():
    cs = bars([100.0] * 200 + [98.0] + [100.0] * 47)      # a dip to 97.5 low inside the last 48 bars
    assert swing_stop(cs, "long", unit=2.0) == pytest.approx(97.5 - 0.6)
    assert swing_stop(cs, "short", unit=2.0) == pytest.approx(100.5 + 0.6)


def test_targets_must_lie_at_least_a_day_beyond_entry():
    cs = bars([100.0] * 168)                               # 7-day high is 100.5
    assert structural_targets(cs, "long", entry=101.0, mfe_pct=None, unit=1.0) == []           # above the week's high
    assert structural_targets(cs, "long", entry=99.0, mfe_pct=0.05, unit=1.0)[0] == ("7-day high", 100.5)
    assert structural_targets(cs, "long", entry=99.9, mfe_pct=0.05, unit=1.0) == [("median favorable excursion for this shape", pytest.approx(104.895))]
    assert structural_targets(cs, "short", entry=99.0, mfe_pct=None, unit=1.0) == []


def test_enter_plan_computes_rr_and_passes_when_thin():
    cs = bars([100.0] * 200 + [96.0] + [100.0] * 47)      # swing low 95.5 -> stop 95.5 - 0.6 = 94.9
    good = build_plan(cs, "long", "enter", price=100.0, unit=2.0, ma20=None, shape_label="trend_up", mfe_pct=0.12, slippage_bps=10)
    assert good.action == "enter" and good.entry == pytest.approx(100.1)
    assert good.invalidation == pytest.approx(94.9)
    assert good.target == pytest.approx(100.1 * 1.12)                                # 7-day high too close: dropped
    assert good.rr == pytest.approx((100.1 * 0.12) / (100.1 - 94.9)) and good.recommendation == "enter"
    thin = build_plan(cs, "long", "enter", price=100.0, unit=2.0, ma20=None, shape_label="trend_up", mfe_pct=0.03, slippage_bps=0)
    assert thin.recommendation == "pass" and "R:R" in thin.reason                    # 3 / 5.1 = 0.59 < 1.5: advise pass
    none = build_plan(cs, "long", "enter", price=100.0, unit=2.0, ma20=None, shape_label="chop", mfe_pct=None, slippage_bps=0)
    assert none.target is None and none.recommendation == "pass"
    far = build_plan(bars([100.0] * 100 + [100 + i * 0.2 for i in range(148)]), "long", "enter", price=129.4, unit=2.0,
                     ma20=None, shape_label="trend_up", mfe_pct=0.30, slippage_bps=0)
    assert far.recommendation == "enter" and far.rr >= MIN_RR


def test_wait_plan_pullback_and_breakout_triggers():
    cs = bars([100.0] * 248)
    pull = build_plan(cs, "long", "wait", price=104.0, unit=2.0, ma20=102.0, shape_label="trend_up", mfe_pct=0.1, slippage_bps=0)
    assert pull.trigger_kind == "pullback" and pull.entry == 102.0
    brk = build_plan(cs, "long", "wait", price=100.0, unit=2.0, ma20=None, shape_label="flag_up", mfe_pct=0.1, slippage_bps=0)
    assert brk.trigger_kind == "breakout" and brk.entry == 100.5                     # 24h high
    no_ma = build_plan(cs, "short", "wait", price=100.0, unit=2.0, ma20=101.0, shape_label="chop", mfe_pct=0.1, slippage_bps=0)
    assert no_ma.trigger_kind == "pullback" and no_ma.entry == pytest.approx(101.0)  # 20MA above price: valid for a short


def test_sizing_and_account_guards():
    cs = bars([100.0] * 200 + [96.0] + [100.0] * 47)
    plan = build_plan(cs, "long", "enter", price=100.0, unit=2.0, ma20=None, shape_label="trend_up", mfe_pct=0.3, slippage_bps=0)
    s = size_for(plan, equity=25_000, risk_pct=0.75, open_risk_usd=0.0, daily_loss_remaining_usd=1000.0)
    assert s["riskUsd"] == pytest.approx(187.5)
    assert s["notionalUsd"] == pytest.approx(187.5 / ((100 - 94.9) / 100))
    assert not s["blocked"]
    capped = size_for(plan, equity=25_000, risk_pct=1.0, open_risk_usd=600.0, daily_loss_remaining_usd=1000.0)
    assert capped["blocked"] and "cap" in capped["blocks"][0]                    # 600 + 250 > 750
    daily = size_for(plan, equity=25_000, risk_pct=1.0, open_risk_usd=0.0, daily_loss_remaining_usd=100.0)
    assert daily["blocked"] and "daily loss" in daily["blocks"][0]
