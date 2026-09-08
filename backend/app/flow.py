"""Order flow, liquidity, cross-section and sizing math. Pure functions.

taker ratio    Binance klines carry the volume bought by aggressive (taker) buyers. Its
               share of total volume is the closest thing to order flow without a trade
               stream: above 0.5 buyers were crossing the spread, below 0.5 sellers were.
walk_book      Fill a notional against the depth snapshot level by level and report the
               average price. The difference from mid, in bps, is the slippage you would
               actually pay. A flat 0.20% fee is wrong for thin pairs; this is not.
beta/residual  How much of a coin's move is just BTC. Residual return is the coin's own.
breadth        Share of tracked coins above their moving averages: is it the market or
               the coin?
ewma_vol       RiskMetrics volatility forecast (lambda 0.94 on hourly returns) for
               volatility-targeted sizing.
daily_tracker  Today's paper P&L against the prop rules you configured.
"""
import math

from .models import Candle, Depth


def taker_ratio(candles: list[Candle], n: int) -> float | None:
    """Share of volume that was aggressive buying over the last n bars with data."""
    recent = [c for c in candles[-n:] if c.taker_buy is not None]
    total = sum(c.volume for c in recent)
    return sum(c.taker_buy for c in recent) / total if recent and total > 0 else None


def taker_ratio_series_mean(candles: list[Candle], n: int) -> float | None:
    """Average of per-bar taker ratios over n bars, for a baseline."""
    vals = [c.taker_buy / c.volume for c in candles[-n:] if c.taker_buy is not None and c.volume > 0]
    return sum(vals) / len(vals) if vals else None


def walk_book(levels: list[tuple[float, float]], notional: float) -> dict:
    """Consume `notional` (quote currency) from best level outward. Levels are (price, qty)."""
    remaining, cost, qty = notional, 0.0, 0.0
    for price, size in levels:
        take = min(size, remaining / price)
        cost += take * price
        qty += take
        remaining -= take * price
        if remaining <= 1e-9:
            break
    return {"avgPrice": cost / qty if qty else None, "filledNotional": cost, "filledFraction": cost / notional if notional else 0.0}


def slippage_table(depth: Depth, notionals: list[float]) -> dict | None:
    """Buy and sell slippage from mid in basis points, for each notional in quote currency."""
    if not depth.bids or not depth.asks:
        return None
    mid = (depth.bids[0][0] + depth.asks[0][0]) / 2
    rows = []
    for n in notionals:
        buy, sell = walk_book(depth.asks, n), walk_book(depth.bids, n)
        rows.append({
            "notional": n,
            "buyBps": (buy["avgPrice"] / mid - 1) * 10_000 if buy["avgPrice"] else None,
            "sellBps": (1 - sell["avgPrice"] / mid) * 10_000 if sell["avgPrice"] else None,
            "buyFilled": buy["filledFraction"], "sellFilled": sell["filledFraction"],
        })
    return {"mid": mid, "levels": len(depth.bids), "rows": rows}


def log_returns_aligned(a: list[Candle], b: list[Candle]) -> tuple[list[float], list[float]]:
    """Log returns of two candle series on their common open_times."""
    bc = {c.open_time: c.close for c in b}
    pairs = [(c.close, bc[c.open_time]) for c in a if c.open_time in bc]
    ra = [math.log(pairs[i][0] / pairs[i - 1][0]) for i in range(1, len(pairs))]
    rb = [math.log(pairs[i][1] / pairs[i - 1][1]) for i in range(1, len(pairs))]
    return ra, rb


def beta(asset_rets: list[float], mkt_rets: list[float]) -> float | None:
    n = len(asset_rets)
    if n < 10 or n != len(mkt_rets):
        return None
    ma, mm = sum(asset_rets) / n, sum(mkt_rets) / n
    cov = sum((a - ma) * (m - mm) for a, m in zip(asset_rets, mkt_rets)) / n
    var = sum((m - mm) ** 2 for m in mkt_rets) / n
    return cov / var if var > 0 else None


def ewma_vol(log_rets: list[float], lam: float = 0.94) -> float | None:
    """RiskMetrics: sigma^2_t = lam * sigma^2_{t-1} + (1-lam) * r^2_t. Returns per-bar sigma."""
    if len(log_rets) < 20:
        return None
    v = sum(r * r for r in log_rets[:20]) / 20
    for r in log_rets[20:]:
        v = lam * v + (1 - lam) * r * r
    return math.sqrt(v)


def breadth(rows: list[dict]) -> dict:
    """rows carry ma20Pct/ma50Pct/ma200Pct/change24hPct per tracked coin."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    share = lambda key: sum(1 for r in rows if r.get(key) is not None and r[key] > 0) / n
    return {"n": n, "above20": share("ma20Pct"), "above50": share("ma50Pct"), "above200": share("ma200Pct"),
            "up24h": share("change24hPct")}


def daily_tracker(closed_today_pnl: float, open_unrealized: float, equity_start: float,
                  daily_limit_pct: float, max_dd_pct: float, peak_equity: float, equity_now: float) -> dict:
    """Where today's paper account stands against the prop rules."""
    today = closed_today_pnl + open_unrealized
    today_pct = today / equity_start * 100
    dd_pct = (peak_equity - equity_now) / peak_equity * 100 if peak_equity > 0 else 0.0
    return {
        "todayPnlUsd": today, "todayPnlPct": today_pct, "dailyLimitPct": daily_limit_pct,
        "dailyBudgetUsedPct": max(0.0, -today_pct) / daily_limit_pct * 100 if daily_limit_pct else None,
        "drawdownPct": dd_pct, "maxDrawdownLimitPct": max_dd_pct,
        "drawdownUsedPct": dd_pct / max_dd_pct * 100 if max_dd_pct else None,
        "breached": today_pct <= -daily_limit_pct or dd_pct >= max_dd_pct,
    }
