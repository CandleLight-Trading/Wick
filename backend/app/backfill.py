"""Gap detection and repair via REST /klines.

Three entry points, all ending in fetch_range():
  sync_recent()  on every (re)connect: last stored closed candle -> now
  extend_back()  first run only: pull deep history for the base-rate intervals
  request_gap()  from the live state machine when it sees a jump in open_time

Never assume continuity after a reconnect. The WebSocket does not replay what you
missed; only REST can fill it. Every genuine repair is logged and kept in `repairs`
so /api/status can show you what happened.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from .adapters.base import ExchangeAdapter
from .rest_client import BannedError
from .store import Store
from .timeutil import floor_to_interval, interval_to_ms, now_ms

log = logging.getLogger(__name__)

PAGE = 1000   # Binance's per-request cap on klines


def plan_range(last_closed_open_time: int | None, interval_ms: int, now: int, lookback_ms: int) -> tuple[int, int] | None:
    """Return the [start, end) range of open_times to request so the store is continuous
    up to and including the candle currently forming. None if nothing is missing.

    Pure function so the tests can hammer it without a network."""
    end = floor_to_interval(now, interval_ms) + interval_ms        # exclusive: one past the forming candle
    oldest_wanted = floor_to_interval(now - lookback_ms, interval_ms)
    if last_closed_open_time is None:
        start = oldest_wanted
    else:
        start = max(last_closed_open_time + interval_ms, oldest_wanted)
    return (start, end) if start < end else None


def find_holes(open_times: list[int], step: int) -> list[tuple[int, int]]:
    """Given sorted open_times of stored closed candles, return every missing [start, end)."""
    return [(a + step, b) for a, b in zip(open_times, open_times[1:]) if b - a > step]


_FROM_STORE = object()   # sentinel: "read last_closed from the store"
MAX_ATTEMPTS = 3


class Backfiller:
    def __init__(self, adapter: ExchangeAdapter, store: Store,
                 on_history_changed: Callable[[str, str], None] | None = None):
        self.adapter = adapter
        self.store = store
        self.on_history_changed = on_history_changed
        self.repairs: list[dict] = []          # audit trail of genuine gaps
        self._queue: asyncio.Queue[tuple[str, str, int, int]] = asyncio.Queue()
        self._queued: set[tuple[str, str, int, int]] = set()
        self._attempts: dict[tuple[str, str, int, int], int] = {}

    async def fetch_range(self, symbol: str, interval: str, start: int, end: int) -> int:
        """Fetch every candle with open_time in [start, end), paging through the 1000-bar cap."""
        step = interval_to_ms(interval)
        cursor, total = start, 0
        while cursor < end:
            batch = await self.adapter.fetch_klines(symbol, interval, cursor, end - 1, PAGE)
            if not batch:
                break
            await self.store.upsert_many(batch)
            total += len(batch)
            cursor = batch[-1].open_time + step
            if len(batch) < PAGE:
                break
        if total and self.on_history_changed:
            self.on_history_changed(symbol, interval)
        return total

    async def sync_recent(self, symbol: str, interval: str, lookback_ms: int, last_closed=_FROM_STORE) -> int:
        """Fill everything between the last stored closed candle and now.

        `last_closed` should be captured BEFORE live frames start flowing: the live feed
        writes new candles at the tail immediately, and if we read the store afterwards
        it looks continuous when it is not. The ingest loop snapshots it at connect time."""
        step = interval_to_ms(interval)
        last = self.store.last_closed_open_time(symbol, interval) if last_closed is _FROM_STORE else last_closed
        rng = plan_range(last, step, now_ms(), lookback_ms)
        if rng is None:
            return 0
        n = await self.fetch_range(symbol, interval, *rng)
        # A first load is not a "gap". A range that spans more than the forming candle is.
        missing_closed = (rng[1] - rng[0]) // step - 1
        if last is not None and missing_closed > 0:
            self._record(symbol, interval, rng[0], rng[1], n, "reconnect")
        return n

    async def ensure_taker_flow(self, symbol: str, interval: str = "1h", bars: int = 720) -> int:
        """Rows written before taker-buy volume was stored have tb = NULL. One REST page
        refreshes the last `bars` candles so the flow metrics have a baseline."""
        last = self.store.latest(symbol, interval, 2)
        if not last or last[0].taker_buy is not None:
            return 0
        step = interval_to_ms(interval)
        end = floor_to_interval(now_ms(), step) + step
        return await self.fetch_range(symbol, interval, end - bars * step, end)

    async def extend_back(self, symbol: str, interval: str, lookback_ms: int) -> int:
        """Pull older history down to now - lookback. Used for the deep 1h/1d series."""
        step = interval_to_ms(interval)
        earliest = await self.store.earliest_open_time(symbol, interval)
        target = floor_to_interval(now_ms() - lookback_ms, step)
        if earliest is None or earliest <= target:
            return 0
        n = await self.fetch_range(symbol, interval, target, earliest)
        log.info("deep history %s %s: +%d candles back to %d", symbol, interval, n, target)
        return n

    async def repair_holes(self, symbol: str, interval: str) -> int:
        """Self-healing pass: scan stored closed candles for missing open_times and queue
        each hole. Catches whatever the connect-time sync could not know about: a crash
        mid-backfill, a failed repair, a live candle written ahead of an unfilled range."""
        step = interval_to_ms(interval)
        holes = find_holes(await self.store.closed_open_times(symbol, interval), step)
        for start, end in holes:
            self.request_gap(symbol, interval, start, end)
        if holes:
            log.info("%s %s: %d hole(s) in stored history queued for repair", symbol, interval, len(holes))
        return len(holes)

    def request_gap(self, symbol: str, interval: str, start: int, end: int):
        """Called by the ingest loop (sync context) when the state machine reports a gap."""
        key = (symbol, interval, start, end)
        if key not in self._queued:
            self._queued.add(key)
            self._queue.put_nowait(key)

    async def worker(self):
        while True:
            key = await self._queue.get()
            self._queued.discard(key)
            symbol, interval, start, end = key
            try:
                n = await self.fetch_range(symbol, interval, start, end)
                self._record(symbol, interval, start, end, n, "live")
                self._attempts.pop(key, None)
            except BannedError:
                log.critical("gap repair halted: IP banned")
            except Exception:
                attempts = self._attempts[key] = self._attempts.get(key, 0) + 1
                log.exception("gap repair failed for %s %s [%d, %d), attempt %d", symbol, interval, start, end, attempts)
                if attempts < MAX_ATTEMPTS:
                    await asyncio.sleep(10)
                    self.request_gap(symbol, interval, start, end)

    def _record(self, symbol, interval, start, end, n, source):
        entry = {"symbol": symbol, "interval": interval, "start": start, "end": end,
                 "candles": n, "source": source, "at": now_ms()}
        self.repairs.append(entry)
        del self.repairs[:-200]   # keep the tail only
        if n == 0:
            # The exchange itself has no candles here (maintenance window). Nothing to repair.
            log.info("gap %s %s [%d, %d): exchange returned no candles", symbol, interval, start, end)
        else:
            log.warning("GAP REPAIRED %s %s [%d, %d): %d candles (%s)", symbol, interval, start, end, n, source)
