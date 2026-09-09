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
    answer = {"context": "adverse", "confidence": "medium", "action_modifier": "weaken", "key_reason": "Large unlock this week.",
              "catalysts": [{"event": "Token unlock", "impact": "negative", "published_at": "2026-09-08", "age_hours": 18, "url": "https://x/y"}],
              "summary": "s"}
    body = {"model": "gpt-x", "usage": {"total_tokens": 900}, "output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer),
                                          "annotations": [{"type": "url_citation", "url": "https://x/y", "title": "Y"}]}]},
    ]}
    out = parse_response(body)
    assert out["context"] == "adverse" and out["citations"] == [{"url": "https://x/y", "title": "Y"}]
    assert out["usage"]["total_tokens"] == 900 and out["model"] == "gpt-x"
    assert set(SCHEMA["required"]) <= set(answer)


def _features_for(mv, vol, rsi_hint, tr, funding=0.0001, ma20=1.0, ma50=2.0, ma200=5.0, residual=None):
    from app.scanner import Features
    return Features("X", 100.0, mv * 3, mv, 3.0, vol, rsi_hint, ma20, ma50, ma200, funding, 1e6, None, tr, 0.5, 1.0, residual, 2.0)


def test_playbooks_pick_the_first_match_and_label_risk():
    from app.scanner import rules_verdict
    # Big move on big volume with one-sided flow, but RSI too hot for trend continuation: Momentum Breakout, aggressive.
    v = rules_verdict(_features_for(mv=2.0, vol=3.0, rsi_hint=80, tr=0.62))
    assert v["stance"] == "long" and v["playbook"] == "Momentum Breakout" and v["riskCharacter"] == "aggressive" and v["maxRiskPct"] == 0.5
    # Larger trend up, price back at the 20-bar mean with RSI reset, no volume: Pullback Continuation, standard.
    v = rules_verdict(_features_for(mv=0.2, vol=0.8, rsi_hint=45, tr=0.5, ma20=0.3))
    assert v["stance"] == "long" and v["playbook"] == "Pullback Continuation" and v["entryAction"] == "enter"
    # Crowded longs, price falling against them, sellers aggressive: Crowded Squeeze short.
    v = rules_verdict(_features_for(mv=-0.8, vol=1.0, rsi_hint=50, tr=0.4, funding=0.002, ma20=-1, ma50=1, ma200=3))
    assert v["stance"] == "short" and v["playbook"] == "Crowded Squeeze"
    # Nothing matches: flat, with the trend-continuation checks shown and every candidate scored.
    v = rules_verdict(_features_for(mv=0.1, vol=0.9, rsi_hint=50, tr=0.5, ma20=-1, ma50=1, ma200=-1))
    assert v["stance"] == "flat" and v["playbook"] is None and len(v["candidates"]) == 6


async def test_research_row_lets_quant_lead_and_context_modify(tmp_path):
    from app.analysis import AnalysisService
    from app.store import Store
    store = Store(tmp_path / "a.db", ring_size=10)
    await store.open()
    a = AnalysisService(store, judge=type("J", (), {"status": {"configured": True}, "model": "m"})(), tracked_fn=lambda: set(), secondary_name=None)
    f = _features_for(mv=2.0, vol=3.0, rsi_hint=80, tr=0.62)
    from app.scanner import rules_verdict
    a.rules["X"] = rules_verdict(f)
    neutral = {"context": "neutral", "confidence": "low", "action_modifier": "unchanged", "key_reason": "Nothing fresh.", "catalysts": [], "summary": "s"}
    row = a.compose_row("X", f, neutral, "manual")
    assert row["stance"] == "long" and row["action"] == "enter" and row["playbook"] == "Momentum Breakout"   # no news is not a veto
    veto = {**neutral, "context": "adverse", "action_modifier": "veto", "key_reason": "Exchange hack.",
            "catalysts": [{"event": "Hot wallet drained", "impact": "negative", "published_at": "u", "age_hours": 3, "url": ""}]}
    row = a.compose_row("X", f, veto, "manual")
    assert row["action"] == "pass" and row["mainRisk"] == "Exchange hack." and row["drivers"][0]["text"] == "Hot wallet drained (3h ago)"
    a.rules["X"] = {"stance": "flat", "entryAction": "enter"}
    assert a.compose_row("X", f, {**neutral, "context": "supportive"}, "manual")["action"] == "wait"
    await store.close()
