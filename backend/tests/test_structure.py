from app.models import Candle
from app.structure import pivots, read_structure

H = 3_600_000


def stairs(n=120, up=True):
    """A zig-zag that makes higher highs and higher lows (or lower/lower)."""
    out, p = [], 100.0
    for i in range(n):
        leg = (i // 10) % 2            # 10 bars with the trend, 10 against, net in the trend's direction
        step = (1.0 if leg == 0 else -0.6) * (1 if up else -1)
        o = p; c = p + step
        out.append(Candle("X", "1h", i * H, o, max(o, c) + 0.2, min(o, c) - 0.2, c, 100 + (i % 7), True))
        p = c
    return out


def test_pivots_label_higher_highs_and_higher_lows():
    sw = pivots(stairs(up=True))
    highs = [p["label"] for p in sw if p["kind"] == "high"][1:]
    lows = [p["label"] for p in sw if p["kind"] == "low"][1:]
    assert highs and all(l == "HH" for l in highs) and lows and all(l == "HL" for l in lows)
    s = read_structure(stairs(up=True))
    assert s["ok"] and s["structure"] == "uptrend" and s["levels"] and all(l["kind"] in ("support", "resistance") for l in s["levels"])
    assert read_structure(stairs(up=False))["structure"] == "downtrend"


def test_breakout_is_reported_when_price_leaves_the_range():
    c = stairs(up=True)
    last = c[-1]
    hi = max(x.high for x in c[-63:-3])
    c[-1] = Candle("X", "1h", last.open_time, last.open, hi + 3, last.open, hi + 2.5, 400, True)
    s = read_structure(c)
    assert any(e["kind"] == "breakout" and e["onVolume"] for e in s["events"])
