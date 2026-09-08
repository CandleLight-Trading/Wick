"""Retention: SQLite is plenty, but not for every 1-minute candle forever.

    1m   7 days      5m   60 days      15m   180 days      1h / 4h / 1d   kept
Equity snapshots older than a week are thinned to one per five minutes.

Never pruned: candles for a symbol that has an open, waiting or ready trade, back to a
day before the earliest such trade. Setups are self-contained snapshots and do not pin raw
candles. The exchange remains the recovery source for ordinary history.
"""
import logging

from . import config
from .store import Store
from .timeutil import now_ms

log = logging.getLogger(__name__)
DAY_MS = 86_400_000


async def prune(store: Store) -> dict:
    now = now_ms()
    active = await store.rows("trades", "state IN ('waiting','ready','open')")
    keep_since: dict[str, int] = {}
    for t in active:
        since = (t["opened_at"] or t["created_at"]) - DAY_MS
        keep_since[t["symbol"]] = min(keep_since.get(t["symbol"], since), since)
    deleted = {}
    for iv, days in config.RETENTION_DAYS.items():
        cutoff = now - days * DAY_MS
        # Symbols with active trades keep everything from before their trade; everyone else prunes at the cutoff.
        n = 0
        async with store._db.execute("SELECT DISTINCT symbol FROM candles WHERE interval=? AND open_time<?", (iv, cutoff)) as cur:
            symbols = [r[0] for r in await cur.fetchall()]
        for sym in symbols:
            limit = min(cutoff, keep_since.get(sym, cutoff))
            cur = await store._db.execute("DELETE FROM candles WHERE symbol=? AND interval=? AND open_time<?", (sym, iv, limit))
            n += cur.rowcount or 0
        deleted[iv] = n
    # Thin old equity snapshots to 5-minute spacing.
    cur = await store._db.execute("DELETE FROM equity_snapshots WHERE time < ? AND (time / 60000) % 5 != 0",
                                  (now - config.EQUITY_THIN_AFTER_DAYS * DAY_MS,))
    deleted["equity_snapshots"] = cur.rowcount or 0
    await store._db.commit()
    if any(deleted.values()):
        log.info("retention pruned %s", deleted)
    return deleted
