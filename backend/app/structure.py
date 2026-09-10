"""Chart structure for the guides layer: what a trader's eye picks out of the candles,
found deterministically so Charts can label it and Learn can explain it.

  swings     pivot highs and lows (a bar higher/lower than K bars either side), labelled
             HH / LH for highs and HL / LL for lows against the previous swing of its kind
  levels     support and resistance: clusters of swing prices within half an ATR, with
             touch counts; the nearest two below and above the last close
  structure  the sequence of the last swings read as uptrend / downtrend / range
  events     breakout or breakdown of the recent range on volume; compression; extension
             from the 20-bar mean in ATR units; a pullback inside a trend

Everything is measured in the interval's own ATR so the same code reads a 1m chart and a
1d chart. Observation only: nothing here predicts.
"""
from .indicators import atr, sma
from .models import Candle

PIVOT_K = 5            # bars either side that must be lower (higher) for a pivot high (low)
LOOKBACK_RANGE = 60    # bars that define "the recent range" for breakouts
CLUSTER_ATR = 0.5      # swing prices within this many ATR belong to one level


def pivots(c: list[Candle], k: int = PIVOT_K) -> list[dict]:
    out = []
    for i in range(k, len(c) - k):
        before, after = c[i - k:i], c[i + 1:i + k + 1]
        # Strictly above everything before, at least equal to everything after: a tie
        # (the next bar opening at the peak) belongs to the earlier bar, once.
        if all(x.high < c[i].high for x in before) and all(x.high <= c[i].high for x in after):
            out.append({"i": i, "time": c[i].open_time, "price": c[i].high, "kind": "high"})
        if all(x.low > c[i].low for x in before) and all(x.low >= c[i].low for x in after):
            out.append({"i": i, "time": c[i].open_time, "price": c[i].low, "kind": "low"})
    out.sort(key=lambda p: p["i"])
    last = {"high": None, "low": None}
    for p in out:
        prev = last[p["kind"]]
        if prev is None:
            p["label"] = "H" if p["kind"] == "high" else "L"
        elif p["kind"] == "high":
            p["label"] = "HH" if p["price"] > prev else "LH"
        else:
            p["label"] = "HL" if p["price"] > prev else "LL"
        last[p["kind"]] = p["price"]
    return out


def levels(swings: list[dict], unit: float, price: float) -> list[dict]:
    """Cluster swing prices; a level is a cluster touched at least twice, or the single
    nearest swing when nothing repeats. Returns up to two below and two above price."""
    if not swings or not unit:
        return []
    pts = sorted(swings, key=lambda p: p["price"])
    clusters: list[list[dict]] = []
    for p in pts:
        if clusters and p["price"] - clusters[-1][-1]["price"] <= CLUSTER_ATR * unit:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    lv = [{"price": sum(x["price"] for x in cl) / len(cl), "touches": len(cl), "lastTime": max(x["time"] for x in cl)} for cl in clusters]
    below = sorted((l for l in lv if l["price"] < price), key=lambda l: (-l["touches"], price - l["price"]))
    above = sorted((l for l in lv if l["price"] > price), key=lambda l: (-l["touches"], l["price"] - price))
    pick = lambda xs, kind: [{**l, "kind": kind} for l in sorted(xs[:2], key=lambda l: l["price"])]
    return pick(below, "support") + pick(above, "resistance")


def read_structure(c: list[Candle]) -> dict:
    if len(c) < 40:
        return {"ok": False, "reason": "not enough candles"}
    closed = [x for x in c if x.closed] or c
    a = atr(closed, 14)[-1] or 0.0
    price = c[-1].close
    sw = pivots(closed)
    recent = sw[-6:]
    highs = [p for p in recent if p["kind"] == "high"][-2:]
    lows = [p for p in recent if p["kind"] == "low"][-2:]
    if len(highs) == 2 and len(lows) == 2 and highs[1]["price"] > highs[0]["price"] and lows[1]["price"] > lows[0]["price"]:
        structure = "uptrend"
    elif len(highs) == 2 and len(lows) == 2 and highs[1]["price"] < highs[0]["price"] and lows[1]["price"] < lows[0]["price"]:
        structure = "downtrend"
    else:
        structure = "range"

    events = []
    body = closed[-LOOKBACK_RANGE - 3:-3] if len(closed) > LOOKBACK_RANGE + 3 else closed[:-3]
    if body:
        rng_hi, rng_lo = max(x.high for x in body), min(x.low for x in body)
        vols = [x.volume for x in closed[-23:-3]]
        vol_avg = sum(vols) / len(vols) if vols else 0
        last3 = closed[-3:]
        recent_vol = max(x.volume for x in last3) if last3 else 0
        if price > rng_hi:
            events.append({"kind": "breakout", "level": rng_hi, "onVolume": bool(vol_avg) and recent_vol >= 1.5 * vol_avg, "time": last3[-1].open_time if last3 else None})
        elif price < rng_lo:
            events.append({"kind": "breakdown", "level": rng_lo, "onVolume": bool(vol_avg) and recent_vol >= 1.5 * vol_avg, "time": last3[-1].open_time if last3 else None})
    if len(closed) >= 80 and a:
        r20 = max(x.high for x in closed[-20:]) - min(x.low for x in closed[-20:])
        r60 = max(x.high for x in closed[-80:-20]) - min(x.low for x in closed[-80:-20])
        if r60 and r20 / r60 < 0.4:
            events.append({"kind": "compression", "ratio": r20 / r60})
    closes = [x.close for x in closed]
    ma20 = sma(closes, 20)[-1] if len(closes) >= 20 else None
    ext = (price - ma20) / a if ma20 is not None and a else None
    if ext is not None and abs(ext) >= 2:
        events.append({"kind": "extension", "atr": ext})
    if ma20 is not None and a and structure in ("uptrend", "downtrend") and abs(price - ma20) <= 0.5 * a:
        # In a trend, price back at the 20-bar mean is a pullback, not a failure.
        events.append({"kind": "pullback", "ma20": ma20})
    if structure == "range" and not any(e["kind"] in ("breakout", "breakdown") for e in events):
        events.append({"kind": "range"})

    return {
        "ok": True, "atr": a, "price": price, "structure": structure,
        "swings": [{k: v for k, v in p.items() if k != "i"} for p in sw[-24:]],
        "levels": levels(sw[-40:], a, price),
        "events": events, "ma20": ma20, "extensionAtr": ext,
    }
