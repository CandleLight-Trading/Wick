"""Planner v1: turns a researched setup into a trade plan. Pure functions.

Two rules, deliberately simple and recorded on every trade as `planner_version` so that
in a month you can ask "did the v1 stop rule work?" instead of untangling doctrine:

  invalidation  the swing low (long) / swing high (short) of the last SWING_BARS bars of
                the setup's timeframe, minus a 0.3 typical-day buffer. That is where the
                thesis is wrong, with room for noise.
  target        the nearer of two structural candidates, each only if it lies beyond the
                entry in the trade direction: the 7-day extreme, and the median favorable
                excursion this shape produced on this coin over the horizon. No candidate
                left means no defensible target, which means PASS.

R:R is computed last. Under MIN_RR the plan carries a PASS recommendation but is still
takeable under Advanced: Wick advises, the trader decides. Account rules are the only
hard block, and they live in desk.py, not here.
"""
from dataclasses import asdict, dataclass

from .models import Candle

PLANNER_VERSION = "v1"
SWING_BARS = 48          # in bars of the setup timeframe (1h -> two days)
BUFFER_UNITS = 0.3       # fraction of one typical day's range
MIN_RR = 1.5
RISK_PROFILES = {"conservative": 0.5, "standard": 0.75, "aggressive": 1.0}   # % of equity at risk per trade
MAX_OPEN_RISK_PCT = 3.0


@dataclass(slots=True)
class Plan:
    side: str                    # long | short
    action: str                  # enter | wait
    entry: float                 # planned fill (enter) or trigger level (wait)
    trigger_kind: str | None     # pullback | breakout | None
    invalidation: float
    target: float | None
    target_source: str | None
    rr: float | None
    recommendation: str          # enter | wait | pass
    reason: str
    planner_version: str = PLANNER_VERSION

    def to_wire(self) -> dict:
        return asdict(self)


def swing_stop(candles: list[Candle], side: str, unit: float) -> float:
    recent = candles[-SWING_BARS:]
    if side == "long":
        return min(c.low for c in recent) - BUFFER_UNITS * unit
    return max(c.high for c in recent) + BUFFER_UNITS * unit


def structural_targets(candles: list[Candle], side: str, entry: float, mfe_pct: float | None, unit: float) -> list[tuple[str, float]]:
    """Candidates beyond entry in the trade direction, nearest first. A candidate closer than
    one typical day's range is noise, not a target (price sitting at the week's high would
    otherwise "target" the next tick)."""
    week = candles[-168:]
    direction = 1 if side == "long" else -1
    cands = []
    extreme = max(c.high for c in week) if side == "long" else min(c.low for c in week)
    cands.append(("7-day high" if side == "long" else "7-day low", extreme))
    if mfe_pct and mfe_pct > 0:
        cands.append(("median favorable excursion for this shape", entry * (1 + direction * mfe_pct)))
    out = [(name, level) for name, level in cands if direction * (level - entry) >= unit]
    out.sort(key=lambda t: direction * t[1])
    return out


def build_plan(candles: list[Candle], side: str, action: str, price: float, unit: float,
               ma20: float | None, shape_label: str | None, mfe_pct: float | None,
               slippage_bps: float | None) -> Plan:
    """`action` is the researched recommendation (enter | wait). `unit` is one typical day's
    range in price. `mfe_pct` is the shape's median favorable excursion as a fraction."""
    direction = 1 if side == "long" else -1
    slip = (slippage_bps or 0) / 10_000
    trigger_kind = None
    if action == "wait":
        if shape_label in ("flag_up", "flag_down"):
            trigger_kind = "breakout"
            day = candles[-24:]
            entry = max(c.high for c in day) if side == "long" else min(c.low for c in day)
        else:
            trigger_kind = "pullback"
            fallback = price - direction * 0.5 * unit
            # The 20-bar mean is the natural pullback level when it sits between price and the stop.
            entry = ma20 if ma20 is not None and (direction * (price - ma20)) > 0 else fallback
    else:
        entry = price * (1 + direction * slip)

    stop = swing_stop(candles, side, unit)
    if direction * (entry - stop) <= 0:
        # Structure is on the wrong side of the entry: fall back to a volatility stop, and say so.
        stop = entry - direction * 1.5 * unit
        stop_note = "volatility stop (no valid swing on the right side)"
    else:
        stop_note = "swing stop"
    risk_per_unit = direction * (entry - stop)

    targets = structural_targets(candles, side, entry, mfe_pct, unit)
    if not targets:
        return Plan(side, action, entry, trigger_kind, stop, None, None, None, "pass",
                    f"No defensible target beyond entry; {stop_note}.")
    source, target = targets[0]
    rr = direction * (target - entry) / risk_per_unit if risk_per_unit > 0 else None
    if rr is None or rr < MIN_RR:
        rec, reason = "pass", f"Reward does not justify planned risk (R:R {rr:.2f} < {MIN_RR}); {stop_note}, target from {source}."
    else:
        rec, reason = action, f"R:R {rr:.2f}; {stop_note}, target from {source}."
    return Plan(side, action, entry, trigger_kind, stop, target, source, rr, rec, reason)


def size_for(plan: Plan, equity: float, risk_pct: float, open_risk_usd: float, daily_loss_remaining_usd: float) -> dict:
    """Position size so a stop-out costs risk_pct of equity, then the account guards."""
    direction = 1 if plan.side == "long" else -1
    risk_frac = direction * (plan.entry - plan.invalidation) / plan.entry
    risk_usd = equity * risk_pct / 100
    notional = risk_usd / risk_frac if risk_frac > 0 else 0.0
    after = open_risk_usd + risk_usd
    cap = equity * MAX_OPEN_RISK_PCT / 100
    # The account's true ceiling: the largest position whose stop-out fits both the open-risk
    # cap and today's remaining loss budget. Wick recommends `notional`; the trader may go
    # up to `max_notional` and not a dollar further.
    max_risk = max(0.0, min(cap - open_risk_usd, daily_loss_remaining_usd))
    max_notional = max_risk / risk_frac if risk_frac > 0 else 0.0
    blocks = []
    if after > cap + 1e-9:
        blocks.append(f"portfolio open risk after entry {after / equity * 100:.2f}% exceeds the {MAX_OPEN_RISK_PCT}% cap")
    if risk_usd > daily_loss_remaining_usd + 1e-9:
        blocks.append(f"a stop-out (${risk_usd:.0f}) would breach today's remaining daily loss budget (${daily_loss_remaining_usd:.0f})")
    return {"notionalUsd": notional, "riskUsd": risk_usd, "riskPct": risk_pct, "riskFrac": risk_frac,
            "maxNotionalUsd": max_notional, "maxRiskUsd": max_risk,
            "openRiskAfterPct": after / equity * 100, "openRiskCapPct": MAX_OPEN_RISK_PCT,
            "blocked": bool(blocks), "blocks": blocks}
