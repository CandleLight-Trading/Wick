"""Scanner, rules judge, paper P&L and the Responses API parser. All pure."""
import json

import pytest

from app.llm import SCHEMA, parse_response
from app.models import Candle, Funding, Ticker
from app.paper import find_exit, forward_returns, pnl_pct, scoreboard
from app.scanner import compute_features, rules_verdict, unusualness

H = 3_600_000
D = 86_400_000
T0 = 1_700_006_400_000


def hourly(n, start=100.0, step=0.0, vol=10.0, zig=0.0):
    """Optionally zig-zag (+zig on odd bars) so RSI is not pinned at 100 on a rising series."""
    out = []
    for i in range(n):
        c = start + i * step + (zig if i % 2 else 0.0)
        out.append(Candle("X", "1h", T0 + i * H, c, c + 0.5, c - 0.5, c, vol, True, taker_buy=vol * 0.6))
    return out


def daily(n, close=100.0, rng=3.0, vol=240.0):
    return [Candle("X", "1d", T0 + i * D, close, close + rng / 2, close - rng / 2, close, vol, True) for i in range(n)]


def test_features_and_unusualness_scale_with_the_coins_own_volatility():
    h1 = hourly(300, vol=20.0)                                     # last 24h volume = 480 vs 240/day avg -> 2x
    ticker = Ticker("X", 106.0, 6.0, 1e6, 107, 99, 0)              # +6% today, daily ATR ~3 -> 2 ATR
    f = compute_features("X", h1, daily(40), ticker, None, None)
    assert f.volMultiple == pytest.approx(2.0)
    assert f.moveAtr == pytest.approx(6.0 / (3.0 / 106.0 * 100), rel=0.01)   # move% / ATR% (ATR% uses current price)
    assert unusualness(f) == pytest.approx(f.moveAtr + 1.0, rel=0.01)       # + (2x - 1) volume
    calm = compute_features("X", hourly(300, vol=10.0), daily(40), Ticker("X", 100.5, 0.5, 1e6, 101, 99, 0), None, None)
    assert unusualness(calm) < 0.5


def test_rules_judge_goes_long_only_when_every_check_passes():
    h1 = hourly(300, start=90.0, step=0.05, vol=20.0, zig=0.3)   # rising with pullbacks: above all MAs, RSI ~58
    price = h1[-1].close * 1.03
    ticker = Ticker("X", price, 3.0, 1e6, price, 99, 0)
    d1 = daily(40, close=price / 1.03, rng=3.0)
    f = compute_features("X", h1, d1, ticker, Funding("X", "t", price, price, 0.0001, None, 1.0, 0), None)
    v = rules_verdict(f)
    assert v["stance"] == "long" and v["trend"] == "up"
    assert v["invalidation"] < price and v["suggestedNotionalPct"] > 0
    assert v["volTargetNotionalPct"] > 0
    assert all(c["ok"] for c in v["checks"])
    assert f.takerBuyRatio24 == pytest.approx(0.6) and f.ewmaDailyVolPct > 0
    # Same setup but funding says longs are crowded: flat, and the UI can see which check failed.
    f2 = compute_features("X", h1, d1, ticker, Funding("X", "t", price, price, 0.001, None, 1.0, 0), None)
    v2 = rules_verdict(f2)
    assert v2["stance"] == "flat"
    assert [c["name"] for c in v2["checks"] if not c["ok"]] == ["Funding not crowded"]


def test_paper_exit_invalidation_beats_horizon_and_pnl_is_net():
    candles = hourly(80)
    candles[10] = Candle("X", "1h", T0 + 10 * H, 100, 100.5, 96.0, 100, 1, True)   # low touches the stop
    ex = find_exit("long", T0, 97.0, 48, candles)
    assert ex.reason == "invalidation" and ex.price == 97.0 and ex.time == T0 + 10 * H
    assert find_exit("long", T0, 50.0, 48, candles).reason == "horizon"
    assert find_exit("long", T0 + 70 * H, 50.0, 48, candles) is None                # still open
    # Long from 100 to 103 over 8h with +0.01%/8h funding: 3% - 0.2% fees - 0.01% funding.
    assert pnl_pct("long", 100.0, 103.0, 8.0, 0.0001) == pytest.approx(0.03 - 0.002 - 0.0001)
    assert pnl_pct("short", 100.0, 103.0, 8.0, 0.0001) == pytest.approx(-0.03 - 0.002 + 0.0001)
    rets = forward_returns("short", T0, 100.0, hourly(80, start=100.0, step=1.0))
    assert rets["ret_1h"] == pytest.approx(-0.01) and rets["ret_72h"] == pytest.approx(-0.72)


def test_scoreboard_reports_prop_style_stats():
    closed = [{"closed_at": T0 + i * D, "pnl_pct": p} for i, p in enumerate([0.02, -0.01, 0.03, -0.04])]
    s = scoreboard(closed)
    assert s["n"] == 4 and s["hitRate"] == 0.5
    assert s["equity"] == pytest.approx(10_000 + 1000 * 0.0)
    assert s["maxDrawdownPct"] > 0 and s["worstDayPct"] == pytest.approx(-0.4)
    assert scoreboard([])["n"] == 0


def test_parse_responses_api_body_with_citations():
    answer = {"stance": "short", "action": "wait", "thesis_verdict": "strengthened", "main_risk": "squeeze",
              "confidence": "medium", "horizon_hours": 24, "invalidation": 81000.0,
              "trend_type": "breakdown", "summary": "s", "drivers": [{"text": "ETF outflows", "url": "https://x/y"}],
              "risks": ["squeeze"], "numbers_used": ["funding +0.007%"]}
    body = {"model": "gpt-x", "usage": {"total_tokens": 900}, "output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer),
                                          "annotations": [{"type": "url_citation", "url": "https://x/y", "title": "Y"}]}]},
    ]}
    out = parse_response(body)
    assert out["stance"] == "short" and out["citations"] == [{"url": "https://x/y", "title": "Y"}]
    assert out["usage"]["total_tokens"] == 900 and out["model"] == "gpt-x"
    assert set(SCHEMA["required"]) <= set(answer)
    with pytest.raises(RuntimeError):
        parse_response({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})
