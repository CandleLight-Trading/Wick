"""Recommendation log and paper trading. Pure functions; the store does the I/O.

Every stance from either judge becomes a fixed-size paper position at the price at the
time of the call. It closes when the invalidation is touched (candle low/high), when the
horizon expires, or when the same judge flips its stance. P&L is net of the 0.20% round
trip and of funding at the rate seen at entry. Because every call is written before the
outcome is known, the scoreboard is out-of-sample by construction.
"""
from dataclasses import dataclass

from .config import PAPER_NOTIONAL, PAPER_START_EQUITY, ROUND_TRIP_COST
from .models import Candle

H_MS = 3_600_000


@dataclass(slots=True)
class Exit:
    time: int
    price: float
    reason: str      # invalidation | horizon | flip | flat


def find_exit(stance: str, entry_time: int, invalidation: float | None, horizon_h: int, candles: list[Candle]) -> Exit | None:
    """Walk closed candles after entry. Invalidation touch beats horizon within the same bar,
    because that is the conservative assumption."""
    deadline = entry_time + horizon_h * H_MS
    for c in candles:
        if c.open_time < entry_time:
            continue
        if invalidation is not None:
            if stance == "long" and c.low <= invalidation:
                return Exit(c.open_time, invalidation, "invalidation")
            if stance == "short" and c.high >= invalidation:
                return Exit(c.open_time, invalidation, "invalidation")
        if c.open_time + H_MS >= deadline:
            return Exit(c.open_time, c.close, "horizon")
    return None


def pnl_pct(stance: str, entry: float, exit_price: float, hours_held: float, funding_rate: float | None) -> float:
    """Return on notional, net of fees and funding. Positive funding: longs pay."""
    direction = 1 if stance == "long" else -1
    gross = direction * (exit_price / entry - 1)
    funding = (funding_rate or 0.0) * (hours_held / 8) * direction
    return gross - ROUND_TRIP_COST - funding


def forward_returns(stance: str, entry_time: int, entry: float, candles: list[Candle]) -> dict[str, float | None]:
    """Signed forward returns at fixed horizons, for the hit-rate table (gross, like the base rates' gross)."""
    direction = 1 if stance == "long" else -1
    close_at = {c.open_time: c.close for c in candles}
    out = {}
    for label, h in (("ret_1h", 1), ("ret_4h", 4), ("ret_24h", 24), ("ret_72h", 72)):
        px = close_at.get(entry_time + h * H_MS)
        out[label] = direction * (px / entry - 1) if px else None
    return out


def scoreboard(closed: list[dict]) -> dict:
    """Equity curve and the stats a prop challenge cares about, from closed paper trades."""
    if not closed:
        return {"n": 0, "equity": PAPER_START_EQUITY}
    equity = PAPER_START_EQUITY
    peak, max_dd = equity, 0.0
    by_day: dict[str, float] = {}
    curve = []
    wins = 0
    for t in sorted(closed, key=lambda x: x["closed_at"]):
        pnl = t["pnl_pct"] * PAPER_NOTIONAL
        equity += pnl
        wins += pnl > 0
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        day = t["closed_at"] // 86_400_000
        by_day[day] = by_day.get(day, 0.0) + pnl
        curve.append({"time": t["closed_at"] // 1000, "equity": round(equity, 2)})
    total = equity - PAPER_START_EQUITY
    best_day = max(by_day.values()) if by_day else 0.0
    return {
        "n": len(closed), "equity": round(equity, 2), "returnPct": total / PAPER_START_EQUITY * 100,
        "hitRate": wins / len(closed), "maxDrawdownPct": max_dd,
        "worstDayPct": min(by_day.values()) / PAPER_START_EQUITY * 100,
        "bestDayShare": best_day / total if total > 0 else None,     # consistency rule: >40-50% fails some firms
        "curve": curve[-200:],
    }
