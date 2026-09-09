"""Mover scanner and rules judge. Pure functions over Features; no I/O, no LLM.

The scanner answers "what deserves attention right now?" by ranking coins on how unusual
the current state is FOR THAT COIN: a 5% day is routine for a memecoin and a three-sigma
event for BTC, so moves are measured in ATR units and volume as a multiple of its own
30-day average.

The rules judge is the old-school, fully transparent stance: every check is listed with
pass/fail so you can see exactly why it said long, short or flat. It is scored in the
recommendation log exactly like the model's calls.
"""
import math
from dataclasses import asdict, dataclass

from .config import DAILY_LOSS_BUDGET_PCT, FUNDING_CROWDED_LONG, FUNDING_CROWDED_SHORT, REC_HORIZON_H
from .flow import beta, ewma_vol, log_returns_aligned, taker_ratio, taker_ratio_series_mean
from .indicators import atr, pct_distance, rsi, sma
from .models import Candle


@dataclass(slots=True)
class Features:
    symbol: str
    price: float
    change24hPct: float | None       # rolling 24h from the ticker
    moveAtr: float | None            # move since yesterday's UTC close, in daily-ATR units (signed)
    atrDailyPct: float | None
    volMultiple: float | None        # last 24 hourly bars vs mean of last 30 closed days
    rsi: float | None                # RSI(14) on 1h bars
    ma20Pct: float | None            # price distance to MA, percent, on 1h bars
    ma50Pct: float | None
    ma200Pct: float | None
    fundingRate: float | None        # per 8h
    openInterestNotional: float | None
    divergenceBps: float | None      # Kraken minus Binance
    takerBuyRatio24: float | None = None    # share of last-24h volume that was aggressive buying
    takerBuyRatio30d: float | None = None   # baseline: mean per-bar ratio over 30 days
    betaBtc: float | None = None            # 60-day daily-return beta to BTC
    residual24hPct: float | None = None     # 24h change minus beta x BTC's 24h change: the coin's own move
    ewmaDailyVolPct: float | None = None    # RiskMetrics forecast of one day's sigma, percent

    def to_wire(self) -> dict:
        return asdict(self)


def compute_features(symbol: str, h1: list[Candle], d1: list[Candle], ticker, funding, other_ticker,
                     btc_d1: list[Candle] | None = None, btc_ticker=None) -> Features | None:
    """h1/d1 are closed candles oldest-first (the rings are enough: 1500 bars)."""
    if len(h1) < 60:
        return None
    closes = [c.close for c in h1]
    price = ticker.last if ticker else closes[-1]
    b = None
    if btc_d1 and len(d1) >= 20:
        ra, rb = log_returns_aligned(d1[-61:], btc_d1[-61:])
        b = beta(ra, rb)
    residual = None
    if b is not None and ticker and btc_ticker:
        residual = ticker.change_pct - b * btc_ticker.change_pct
    hourly_rets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - 720), len(closes))]
    sig = ewma_vol(hourly_rets)
    days = d1[-30:]
    avg_day = sum(c.volume for c in days) / len(days) if days else None
    last24 = sum(c.volume for c in h1[-24:])
    atr_d = atr(d1)[-1] if len(d1) >= 14 else None
    atr_pct = atr_d / price * 100 if atr_d else None
    today_move = pct_distance(price, d1[-1].close) if d1 else None
    r = rsi(closes)[-1]
    ma = {n: sma(closes, n)[-1] if len(closes) >= n else None for n in (20, 50, 200)}
    return Features(
        symbol=symbol, price=price,
        change24hPct=ticker.change_pct if ticker else None,
        moveAtr=today_move / atr_pct if today_move is not None and atr_pct else None,
        atrDailyPct=atr_pct,
        volMultiple=last24 / avg_day if avg_day else None,
        rsi=r, ma20Pct=pct_distance(price, ma[20]), ma50Pct=pct_distance(price, ma[50]), ma200Pct=pct_distance(price, ma[200]),
        fundingRate=funding.funding_rate if funding else None,
        openInterestNotional=funding.open_interest * funding.mark_price if funding and funding.open_interest and funding.mark_price else None,
        divergenceBps=(other_ticker.last / price - 1) * 10_000 if other_ticker and price else None,
        takerBuyRatio24=taker_ratio(h1, 24), takerBuyRatio30d=taker_ratio_series_mean(h1, 720),
        betaBtc=b, residual24hPct=residual,
        ewmaDailyVolPct=sig * math.sqrt(24) * 100 if sig else None,
    )


def unusualness(f: Features) -> float:
    """Higher = more unusual for this coin. Components are roughly 'sigmas' so they add."""
    s = 0.0
    if f.moveAtr is not None:
        s += abs(f.moveAtr)                       # 1.0 = a full typical day's range
    if f.volMultiple is not None:
        s += max(0.0, f.volMultiple - 1.0)        # 2x normal volume adds 1
    if f.fundingRate is not None:
        s += abs(f.fundingRate) / FUNDING_CROWDED_LONG * 0.5   # crowded funding adds 0.5 per "crowded unit"
    if f.divergenceBps is not None:
        s += min(abs(f.divergenceBps) / 20.0, 1.0)             # cross-venue dislocation, capped
    return round(s, 3)


# ---- playbooks -------------------------------------------------------------------------
# Six ways a coin can be interesting, each with its own checks and its own risk character.
# The first playbook whose checks all pass wins; the others are reported as candidates so
# the UI can show how close they came. STANDARD playbooks size at the trader's profile;
# AGGRESSIVE ones are capped at 0.5% of equity because their failures are violent.
PLAYBOOKS = [
    ("Trend Continuation", "standard"),
    ("Pullback Continuation", "standard"),
    ("Relative Strength", "moderate"),
    ("Momentum Breakout", "aggressive"),
    ("Blow-off Reversal", "aggressive"),
    ("Crowded Squeeze", "aggressive"),
]
PLAYBOOK_MAX_RISK_PCT = {"standard": 0.75, "moderate": 0.75, "aggressive": 0.5}
PLAYBOOK_SUMMARY = {
    "Trend Continuation": "Established trend with volume, aggressors on the trend side, not extended into an extreme.",
    "Pullback Continuation": "Larger trend intact and price has come back to the 20-bar mean with RSI reset.",
    "Relative Strength": "Moving on its own, beyond what BTC explains, with the larger trend behind it.",
    "Momentum Breakout": "Exceptional move on exceptional volume with one-sided flow. Can fail violently; size small.",
    "Blow-off Reversal": "An exhaustion bar with RSI at an extreme and aggressors already flipping. Fade with confirmation only.",
    "Crowded Squeeze": "Funding says one side is crowded and price is moving against that crowd. Squeezes are fast; size small.",
}


def _checks():
    checks = []

    def check(name: str, ok: bool | None, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})
        return bool(ok)
    return checks, check


def _fmt(v, f="{:.2f}", none="n/a"):
    return f.format(v) if v is not None else none


def rules_verdict(f: Features, shape: dict | None = None) -> dict:
    """Transparent stance from the first playbook whose checks all pass. Every check of every
    playbook is reported so the UI can show why, and how close the others came. The shape
    engine adds one veto on top: a shape whose own base rate on this coin is clearly adverse
    (n >= 30 and net hit rate under 40% at +24h in the stance direction) blocks the call."""
    above = [x is not None and x > 0 for x in (f.ma20Pct, f.ma50Pct, f.ma200Pct)]
    below = [x is not None and x < 0 for x in (f.ma20Pct, f.ma50Pct, f.ma200Pct)]
    trend = "up" if all(above) else "down" if all(below) else "mixed"
    big_up = f.ma50Pct is not None and f.ma200Pct is not None and f.ma50Pct > 0 and f.ma200Pct > 0
    big_down = f.ma50Pct is not None and f.ma200Pct is not None and f.ma50Pct < 0 and f.ma200Pct < 0
    crowded_long = f.fundingRate is not None and f.fundingRate > FUNDING_CROWDED_LONG
    crowded_short = f.fundingRate is not None and f.fundingRate < FUNDING_CROWDED_SHORT
    funding_txt = f"{f.fundingRate * 100:+.4f}%/8h" if f.fundingRate is not None else "no perp data"
    tr = f.takerBuyRatio24
    flow_txt = (f"{tr * 100:.0f}% of 24h volume was aggressive buying" + (f" (30d avg {f.takerBuyRatio30d * 100:.0f}%)" if f.takerBuyRatio30d else "")) if tr is not None else "no flow data yet"
    mv = f.moveAtr
    label = shape["label"] if shape else None
    exhausted = label in ("blowoff_up", "capitulation")
    trend_txt = f"{trend}: {f.ma20Pct:+.2f}% / {f.ma50Pct:+.2f}% / {f.ma200Pct:+.2f}%" if f.ma200Pct is not None else "not enough history"

    def trend_continuation():
        checks, check = _checks()
        check("Trend (price vs 20/50/200 MA, 1h bars)", trend != "mixed", trend_txt)
        vol_ok = check("Volume confirms (>= 1.3x 30d avg)", f.volMultiple is not None and f.volMultiple >= 1.3, f"{_fmt(f.volMultiple)}x")
        check("Funding not crowded", not (crowded_long or crowded_short), funding_txt)
        rsi_ok_long = f.rsi is not None and f.rsi < 75
        rsi_ok_short = f.rsi is not None and f.rsi > 25
        check("RSI not at an extreme", (rsi_ok_long if trend == "up" else rsi_ok_short) if f.rsi is not None else None, f"RSI {_fmt(f.rsi, '{:.0f}')}")
        moved = mv is not None and abs(mv) >= 0.5
        check("Move is meaningful (>= 0.5 daily ATR)", moved, f"{_fmt(mv, '{:+.2f}')} ATR")
        flow_long = tr is not None and tr > 0.5
        flow_short = tr is not None and tr < 0.5
        check("Taker flow agrees (aggressors on the trend side)", None if tr is None or trend == "mixed" else (flow_long if trend == "up" else flow_short), flow_txt)
        if trend == "up" and vol_ok and not crowded_long and rsi_ok_long and moved and mv > 0 and flow_long:
            return "long", checks
        if trend == "down" and vol_ok and not crowded_short and rsi_ok_short and moved and mv < 0 and flow_short:
            return "short", checks
        return "flat", checks

    def pullback():
        checks, check = _checks()
        side = "long" if big_up else "short" if big_down else None
        check("Larger trend (price vs 50/200 MA)", side is not None, trend_txt)
        near = f.ma20Pct is not None and f.atrDailyPct and abs(f.ma20Pct) <= 0.5 * f.atrDailyPct
        check("Pulled back to the 20-bar mean (within half a daily ATR)", bool(near), f"{_fmt(f.ma20Pct, '{:+.2f}')}% from MA20, ATR {_fmt(f.atrDailyPct)}%")
        rsi_ok = f.rsi is not None and ((35 <= f.rsi <= 60) if side == "long" else (40 <= f.rsi <= 65) if side == "short" else False)
        check("RSI reset (35-60 long, 40-65 short)", rsi_ok if f.rsi is not None else None, f"RSI {_fmt(f.rsi, '{:.0f}')}")
        check("Funding not crowded", not (crowded_long or crowded_short), funding_txt)
        check("Not an exhaustion bar", not exhausted, shape["name"] if shape else "no shape yet")
        ok = side and near and rsi_ok and not (crowded_long or crowded_short) and not exhausted
        return (side if ok else "flat"), checks

    def relative_strength():
        checks, check = _checks()
        r = f.residual24hPct
        side = "long" if r is not None and r >= 3 else "short" if r is not None and r <= -3 else None
        check("Own move vs BTC (beta-adjusted) >= 3%", side is not None, f"{_fmt(r, '{:+.2f}')}%, beta {_fmt(f.betaBtc)}")
        aligned = (big_up if side == "long" else big_down if side == "short" else False)
        check("Larger trend agrees (50/200 MA)", aligned if side else None, trend_txt)
        vol_ok = f.volMultiple is not None and f.volMultiple >= 1.0
        check("Volume at least average", vol_ok, f"{_fmt(f.volMultiple)}x")
        crowded = crowded_long if side == "long" else crowded_short
        check("Funding not crowded on this side", not crowded, funding_txt)
        rsi_ok = f.rsi is not None and (f.rsi < 80 if side == "long" else f.rsi > 20)
        check("RSI not beyond 80/20", rsi_ok if f.rsi is not None else None, f"RSI {_fmt(f.rsi, '{:.0f}')}")
        ok = side and aligned and vol_ok and not crowded and rsi_ok and not exhausted
        return (side if ok else "flat"), checks

    def momentum_breakout():
        checks, check = _checks()
        side = "long" if mv is not None and mv >= 1.5 else "short" if mv is not None and mv <= -1.5 else None
        check("Move >= 1.5 daily ATR", side is not None, f"{_fmt(mv, '{:+.2f}')} ATR")
        vol_ok = f.volMultiple is not None and f.volMultiple >= 2.0
        check("Volume >= 2x 30d avg", vol_ok, f"{_fmt(f.volMultiple)}x")
        flow_ok = tr is not None and ((tr > 0.55) if side == "long" else (tr < 0.45) if side == "short" else False)
        check("Taker flow strongly one-sided (>55% / <45%)", flow_ok if tr is not None and side else None, flow_txt)
        ma_ok = f.ma20Pct is not None and ((f.ma20Pct > 0) if side == "long" else (f.ma20Pct < 0) if side == "short" else False)
        check("Price on the right side of the 20-bar mean", ma_ok if side else None, f"{_fmt(f.ma20Pct, '{:+.2f}')}% from MA20")
        rsi_ok = f.rsi is not None and (f.rsi < 85 if side == "long" else f.rsi > 15)
        check("RSI not beyond 85/15", rsi_ok if f.rsi is not None else None, f"RSI {_fmt(f.rsi, '{:.0f}')}")
        check("Not an exhaustion bar", not exhausted, shape["name"] if shape else "no shape yet")
        crowded = crowded_long if side == "long" else crowded_short
        check("Funding not crowded on this side", not crowded, funding_txt)
        ok = side and vol_ok and flow_ok and ma_ok and rsi_ok and not exhausted and not crowded
        return (side if ok else "flat"), checks

    def blowoff_reversal():
        checks, check = _checks()
        side = "short" if label == "blowoff_up" else "long" if label == "capitulation" else None
        check("Exhaustion shape present (blow-off or capitulation)", side is not None, shape["name"] if shape else "no shape yet")
        rsi_ok = f.rsi is not None and ((f.rsi > 78) if side == "short" else (f.rsi < 22) if side == "long" else False)
        check("RSI at an extreme (>78 / <22)", rsi_ok if f.rsi is not None and side else None, f"RSI {_fmt(f.rsi, '{:.0f}')}")
        flow_ok = tr is not None and ((tr < 0.5) if side == "short" else (tr > 0.5) if side == "long" else False)
        check("Aggressors already flipping against the move", flow_ok if tr is not None and side else None, flow_txt)
        ok = side and rsi_ok and flow_ok
        return (side if ok else "flat"), checks

    def crowded_squeeze():
        checks, check = _checks()
        side = "short" if crowded_long else "long" if crowded_short else None
        check("Funding crowded on one side", side is not None, funding_txt)
        against = mv is not None and ((mv <= -0.5) if side == "short" else (mv >= 0.5) if side == "long" else False)
        check("Price moving against the crowd (>= 0.5 ATR)", against if side else None, f"{_fmt(mv, '{:+.2f}')} ATR")
        flow_ok = tr is not None and ((tr < 0.5) if side == "short" else (tr > 0.5) if side == "long" else False)
        check("Taker flow with the squeeze", flow_ok if tr is not None and side else None, flow_txt)
        check("Not an exhaustion bar", not exhausted, shape["name"] if shape else "no shape yet")
        ok = side and against and flow_ok and not exhausted
        return (side if ok else "flat"), checks

    evals = {"Trend Continuation": trend_continuation, "Pullback Continuation": pullback, "Relative Strength": relative_strength,
             "Momentum Breakout": momentum_breakout, "Blow-off Reversal": blowoff_reversal, "Crowded Squeeze": crowded_squeeze}
    candidates, stance, playbook, character, checks = [], "flat", None, None, None
    for name, risk in PLAYBOOKS:
        s, cs = evals[name]()
        passed = sum(1 for c in cs if c["ok"]), sum(1 for c in cs if c["ok"] is not None)
        candidates.append({"playbook": name, "riskCharacter": risk, "stance": s, "passed": passed[0], "of": passed[1]})
        if s != "flat" and playbook is None:
            stance, playbook, character, checks = s, name, risk, cs
    if checks is None:
        checks = evals["Trend Continuation"]()[1]          # the default story when nothing matches

    if shape and stance != "flat":
        h24 = shape["horizons"]["24h"]
        hit = h24.get("hitRateNet")                          # forward_stats measures long returns; a short wants the mirror image
        adverse = (not h24["underpowered"] and hit is not None
                   and ((stance == "long" and hit < 0.4) or (stance == "short" and hit > 0.6)))
        detail = f"{shape['name']}" + (f"; on this coin +24h long hit {hit * 100:.0f}% (n={h24['n']}{', too few' if h24['underpowered'] else ''})" if hit is not None else "")
        checks = checks + [{"name": "Shape base rate does not veto", "ok": not adverse, "detail": detail}]
        if adverse:
            stance = "flat"

    # Extended trend entries wait for a pullback; everything else is an entry at market.
    entry_action = "wait" if playbook in ("Trend Continuation", "Relative Strength") and mv is not None and abs(mv) > 1.0 else "enter"
    inv = None
    dist_pct = 1.5 * f.atrDailyPct if f.atrDailyPct else None
    if stance == "long" and dist_pct:
        inv = f.price * (1 - dist_pct / 100)
    elif stance == "short" and dist_pct:
        inv = f.price * (1 + dist_pct / 100)
    return {
        "source": "rules", "stance": stance, "trend": trend, "horizonH": REC_HORIZON_H,
        "playbook": playbook if stance != "flat" else None, "riskCharacter": character if stance != "flat" else None,
        "maxRiskPct": PLAYBOOK_MAX_RISK_PCT.get(character) if stance != "flat" else None,
        "entryAction": entry_action, "candidates": candidates,
        "invalidation": inv, "invalidationPct": dist_pct if stance != "flat" else None,
        # Position size so that a stop-out costs DAILY_LOSS_BUDGET_PCT of the account.
        "suggestedNotionalPct": (DAILY_LOSS_BUDGET_PCT / dist_pct * 100) if stance != "flat" and dist_pct else None,
        # Volatility-targeted alternative: size so that a one-sigma day costs the same budget.
        "volTargetNotionalPct": (DAILY_LOSS_BUDGET_PCT / f.ewmaDailyVolPct * 100) if stance != "flat" and f.ewmaDailyVolPct else None,
        "checks": checks,
        "summary": PLAYBOOK_SUMMARY[playbook] if stance != "flat" and playbook else "No playbook matches; no position.",
    }
