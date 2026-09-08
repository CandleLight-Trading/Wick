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


def rules_verdict(f: Features, shape: dict | None = None) -> dict:
    """Transparent stance. Every check is reported so the UI can show why.
    The shape engine adds one veto: a blow-off or capitulation bar is exhaustion, not a trend
    to join, and a shape whose own base rate on this coin is clearly adverse (n >= 30 and
    net hit rate under 40% at +24h in the stance direction) blocks the call."""
    checks = []

    def check(name: str, ok: bool | None, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})
        return bool(ok)

    above = [x is not None and x > 0 for x in (f.ma20Pct, f.ma50Pct, f.ma200Pct)]
    below = [x is not None and x < 0 for x in (f.ma20Pct, f.ma50Pct, f.ma200Pct)]
    trend = "up" if all(above) else "down" if all(below) else "mixed"
    check("Trend (price vs 20/50/200 MA, 1h bars)", trend != "mixed", f"{trend}: {f.ma20Pct:+.2f}% / {f.ma50Pct:+.2f}% / {f.ma200Pct:+.2f}%" if f.ma200Pct is not None else "not enough history")
    vol_ok = check("Volume confirms (>= 1.3x 30d avg)", f.volMultiple is not None and f.volMultiple >= 1.3, f"{f.volMultiple:.2f}x" if f.volMultiple is not None else "n/a")
    crowded_long = f.fundingRate is not None and f.fundingRate > FUNDING_CROWDED_LONG
    crowded_short = f.fundingRate is not None and f.fundingRate < FUNDING_CROWDED_SHORT
    check("Funding not crowded", not (crowded_long or crowded_short),
          f"{f.fundingRate * 100:+.4f}%/8h" if f.fundingRate is not None else "no perp data")
    rsi_ok_long = f.rsi is not None and f.rsi < 75
    rsi_ok_short = f.rsi is not None and f.rsi > 25
    check("RSI not at an extreme", (rsi_ok_long if trend == "up" else rsi_ok_short) if f.rsi is not None else None,
          f"RSI {f.rsi:.0f}" if f.rsi is not None else "n/a")
    moved = f.moveAtr is not None and abs(f.moveAtr) >= 0.5
    check("Move is meaningful (>= 0.5 daily ATR)", moved, f"{f.moveAtr:+.2f} ATR" if f.moveAtr is not None else "n/a")
    tr = f.takerBuyRatio24
    flow_long = tr is not None and tr > 0.5
    flow_short = tr is not None and tr < 0.5
    check("Taker flow agrees (aggressors on the trend side)",
          None if tr is None or trend == "mixed" else (flow_long if trend == "up" else flow_short),
          f"{tr * 100:.0f}% of 24h volume was aggressive buying" + (f" (30d avg {f.takerBuyRatio30d * 100:.0f}%)" if f.takerBuyRatio30d else "") if tr is not None else "no flow data yet")

    if trend == "up" and vol_ok and not crowded_long and rsi_ok_long and moved and (f.moveAtr or 0) > 0 and flow_long:
        stance = "long"
    elif trend == "down" and vol_ok and not crowded_short and rsi_ok_short and moved and (f.moveAtr or 0) < 0 and flow_short:
        stance = "short"
    else:
        stance = "flat"

    if shape:
        label = shape["label"]
        h24 = shape["horizons"]["24h"]
        exhausted = label in ("blowoff_up", "capitulation")
        # forward_stats measures long returns; a short wants the mirror image.
        hit = h24.get("hitRateNet")
        adverse = (not h24["underpowered"] and hit is not None and stance != "flat"
                   and ((stance == "long" and hit < 0.4) or (stance == "short" and hit > 0.6)))
        ok = None if stance == "flat" else not (exhausted or adverse)
        detail = f"{shape['name']}"
        if hit is not None:
            detail += f"; on this coin +24h long hit {hit * 100:.0f}% (n={h24['n']}{', too few' if h24['underpowered'] else ''})"
        check("Shape does not veto", ok, detail)
        if stance != "flat" and ok is False:
            stance = "flat"

    inv = None
    dist_pct = 1.5 * f.atrDailyPct if f.atrDailyPct else None
    if stance == "long" and dist_pct:
        inv = f.price * (1 - dist_pct / 100)
    elif stance == "short" and dist_pct:
        inv = f.price * (1 + dist_pct / 100)
    return {
        "source": "rules", "stance": stance, "trend": trend, "horizonH": REC_HORIZON_H,
        "invalidation": inv, "invalidationPct": dist_pct if stance != "flat" else None,
        # Position size so that a stop-out costs DAILY_LOSS_BUDGET_PCT of the account.
        "suggestedNotionalPct": (DAILY_LOSS_BUDGET_PCT / dist_pct * 100) if stance != "flat" and dist_pct else None,
        # Volatility-targeted alternative: size so that a one-sigma day costs the same budget.
        "volTargetNotionalPct": (DAILY_LOSS_BUDGET_PCT / f.ewmaDailyVolPct * 100) if stance != "flat" and f.ewmaDailyVolPct else None,
        "checks": checks,
        "summary": {"long": "Uptrend with volume, funding not crowded, shape allows.", "short": "Downtrend with volume, funding not crowded, shape allows.",
                    "flat": "Not all checks pass; no position."}[stance],
    }
