"""One WebSocket connection per exchange, every tracked stream multiplexed on it.

Lifecycle of a connection:
  1. Build the stream set from the tracked symbols (klines x intervals, tickers, depth).
  2. Connect. If the venue needs it, send subscribe messages. Start a background sync
     that backfills from the last stored candle to now.
  3. Consume frames: run each kline through its CandleTracker, write, fan out.
  4. On any error: mark "reconnecting", sleep exponential-backoff-with-jitter, go to 1.

Binance closes every connection after 24h, so step 4 is routine, not exceptional.
A second instance runs for Kraken with tickers only (intervals=[], no backfiller).
"""
import asyncio
import logging
import random
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .adapters.base import ExchangeAdapter
from .backfill import Backfiller
from .broadcaster import Broadcaster
from .candle_state import Action, CandleTracker
from .config import DEEP_HISTORY_MS, INTERVALS, LOG_WS_FRAMES, RING_SIZE
from .models import Depth, KlineFrame, Ticker
from .rest_client import BannedError
from .store import Store
from .timeutil import interval_to_ms, now_ms

log = logging.getLogger(__name__)

OnClose = Callable[[str, str], Awaitable[None]]


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """1, 2, 4, 8 ... seconds capped at `cap`, then scaled by uniform(0.5, 1.5).
    The jitter stops every client on the internet reconnecting in the same second."""
    return min(cap, base * 2 ** attempt) * random.uniform(0.5, 1.5)


class IngestService:
    def __init__(self, adapter: ExchangeAdapter, store: Store, backfiller: Backfiller | None,
                 broadcaster: Broadcaster, tracked: list[str], on_close: OnClose | None = None,
                 intervals: list[str] = INTERVALS, ticker_sink: dict[str, Ticker] | None = None):
        self.adapter = adapter
        self.store = store
        self.backfiller = backfiller
        self.broadcaster = broadcaster
        self.on_close = on_close
        self.intervals = list(intervals)
        self.tickers = store.tickers if ticker_sink is None else ticker_sink
        self.tracked: set[str] = set(tracked)
        self.depth_symbols: set[str] = set()      # only what an open Context panel needs
        self.state = "connecting"                 # connecting | live | reconnecting
        self.last_frame_ms = 0
        self.reconnects = 0
        self.frames = 0
        self.rejected = 0
        self._trackers: dict[tuple[str, str], CandleTracker] = {}
        self._ws = None
        self._streams: set[str] = set()
        self._req_id = 0
        # ALIVE vs READY: the process serves pages immediately; this is set once stored
        # history is continuous up to now, after which scanning and trade actions begin.
        self.history_ready = asyncio.Event()
        self.sync_progress = {"done": 0, "total": 0}
        if LOG_WS_FRAMES:
            logging.getLogger("websockets.client").setLevel(logging.DEBUG)

    # ---- stream set ---------------------------------------------------------
    def desired_streams(self) -> set[str]:
        s = set()
        for sym in self.tracked:
            s.add(self.adapter.ticker_stream(sym))
            for iv in self.intervals:
                s.add(self.adapter.kline_stream(sym, iv))
        for sym in self.depth_symbols:
            s.add(self.adapter.depth_stream(sym))
        return s

    # ---- main loop ------------------------------------------------------------
    async def run(self):
        attempt = 0
        while True:
            streams = sorted(self.desired_streams())
            url = self.adapter.ws_url(streams)
            try:
                # ping_interval/ping_timeout: WE ping every 20s and drop the socket if no pong
                # arrives in 20s. That is our liveness check. The venue's own pings are answered by
                # the library's protocol layer (websockets/protocol.py: OP_PING -> queue OP_PONG);
                # flip LOG_WS_FRAMES in config.py to watch "% received ping" / "> PONG" lines.
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=4096) as ws:
                    self._ws, self._streams = ws, set(streams)
                    self._trackers.clear()      # trackers restart; the store, not the tracker, holds history
                    self.state, attempt = "live", 0
                    log.info("connected to %s with %d streams", self.adapter.name, len(streams))
                    if self.adapter.subscribe_on_connect and streams:
                        self._req_id += 1
                        for m in self.adapter.subscribe_messages(streams, self._req_id):
                            await ws.send(m)
                    # Snapshot "where does stored history end" NOW, before the first live frame
                    # lands in the store and makes it look continuous.
                    sync = asyncio.create_task(self._sync_all(self._snapshot_last_closed(self.tracked)))
                    try:
                        async for raw in ws:
                            await self._handle(raw)
                    finally:
                        sync.cancel()
            except (ConnectionClosed, InvalidHandshake, OSError, asyncio.TimeoutError) as e:
                log.warning("%s websocket dropped: %r", self.adapter.name, e)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unexpected %s ingest error", self.adapter.name)
            self._ws = None
            self.state = "reconnecting"
            self.reconnects += 1
            delay = backoff_delay(attempt)
            attempt += 1
            log.info("%s reconnecting in %.1fs (attempt %d)", self.adapter.name, delay, attempt)
            await asyncio.sleep(delay)

    def _snapshot_last_closed(self, symbols) -> dict[tuple[str, str], int | None]:
        """No awaits in here: it must run atomically with respect to incoming frames."""
        return {(sym, iv): self.store.last_closed_open_time(sym, iv) for sym in symbols for iv in self.intervals}

    async def _sync_all(self, snapshot: dict[tuple[str, str], int | None]):
        """Shallow backfill for every tracked symbol first so charts fill in seconds,
        then the deep 1h/1d history the context panel needs, then a hole scan.

        Retries the whole pass on failure. Every step is idempotent (upserts), so a
        retry only costs the REST weight of the ranges already fetched."""
        if self.backfiller is None or not self.intervals:
            self.history_ready.set()
            return
        self.sync_progress = {"done": 0, "total": len(self.tracked) * len(self.intervals)}
        for attempt in range(6):
            try:
                for sym in sorted(self.tracked):
                    await self._sync_symbol(sym, snapshot)
                for sym in sorted(self.tracked):
                    await self._deep_symbol(sym)
                for sym in sorted(self.tracked):
                    for iv in self.intervals:
                        await self.backfiller.repair_holes(sym, iv)
                log.info("history sync complete")
                self.history_ready.set()       # ALIVE became READY: scanner and trade actions may start
                return
            except BannedError:
                log.critical("backfill halted: IP banned")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                wait = 30 * (attempt + 1)
                log.exception("history sync failed; retrying in %ds", wait)
                await asyncio.sleep(wait)
        log.error("history sync gave up; will retry on next reconnect")

    async def _sync_symbol(self, sym: str, snapshot: dict[tuple[str, str], int | None]):
        for iv in self.intervals:
            lookback = RING_SIZE * interval_to_ms(iv)
            await self.backfiller.sync_recent(sym, iv, lookback, last_closed=snapshot.get((sym, iv)))
            await self.backfiller.extend_back(sym, iv, lookback)   # no-op once the ring depth is stored
            self.sync_progress["done"] = min(self.sync_progress["total"], self.sync_progress["done"] + 1)
        await self.backfiller.ensure_taker_flow(sym)               # no-op once the tb column is populated

    async def _deep_symbol(self, sym: str):
        for iv, lookback in DEEP_HISTORY_MS.items():
            if iv in self.intervals:
                await self.backfiller.extend_back(sym, iv, lookback)

    # ---- frame handling ---------------------------------------------------------
    async def _handle(self, raw: str):
        ev = self.adapter.parse_message(raw)
        if ev is None:
            return
        self.frames += 1
        self.last_frame_ms = now_ms()
        if isinstance(ev, KlineFrame):
            await self._on_kline(ev)
        elif isinstance(ev, Ticker):
            self.tickers[ev.symbol] = ev
            self.broadcaster.publish_ticker(ev)
        elif isinstance(ev, Depth):
            self.store.depth_live[ev.symbol] = ev
            self.broadcaster.publish_depth(ev)

    async def _on_kline(self, frame: KlineFrame):
        c = frame.candle
        key = (c.symbol, c.interval)
        tracker = self._trackers.get(key)
        if tracker is None:
            tracker = self._trackers[key] = CandleTracker(interval_to_ms(c.interval))
        decision = tracker.on_frame(c, frame.event_time)
        if decision.action is Action.REJECT_STALE:
            self.rejected += 1
            return
        if decision.gap and self.backfiller:
            self.backfiller.request_gap(c.symbol, c.interval, *decision.gap)
        await self.store.upsert(c)
        self.broadcaster.publish_kline(c)
        if decision.action is Action.CLOSE and self.on_close:
            await self.on_close(c.symbol, c.interval)

    # ---- subscription changes -------------------------------------------------
    async def set_tracked(self, symbols: set[str]):
        added = symbols - self.tracked
        self.tracked = set(symbols)
        for sym in added:
            for iv in self.intervals:
                await self.store.reload_ring(sym, iv)   # it may have been tracked before
        snapshot = self._snapshot_last_closed(added)      # before subscribing, so no frame precedes it
        await self._resubscribe()
        for sym in sorted(added):
            asyncio.create_task(self._sync_new(sym, snapshot))

    async def _sync_new(self, sym: str, snapshot: dict[tuple[str, str], int | None]):
        if self.backfiller is None:
            return
        try:
            await self._sync_symbol(sym, snapshot)
            await self._deep_symbol(sym)
            for iv in self.intervals:
                await self.backfiller.repair_holes(sym, iv)
        except Exception:
            log.exception("sync for newly tracked %s failed", sym)

    async def set_depth_symbols(self, symbols: set[str]):
        self.depth_symbols = set(symbols)
        await self._resubscribe()

    async def _resubscribe(self):
        """Diff desired vs current streams and (un)subscribe on the live socket.
        If we are between connections the next connect builds the set from scratch."""
        if self._ws is None:
            return
        desired = self.desired_streams()
        add, remove = sorted(desired - self._streams), sorted(self._streams - desired)
        try:
            if add:
                self._req_id += 1
                for m in self.adapter.subscribe_messages(add, self._req_id):
                    await self._ws.send(m)
            if remove:
                self._req_id += 1
                for m in self.adapter.unsubscribe_messages(remove, self._req_id):
                    await self._ws.send(m)
        except ConnectionClosed:
            log.warning("socket closed mid-resubscribe; the reconnect will pick up the new stream set")
            return
        self._streams = desired
        for key in list(self._trackers):
            if self.adapter.kline_stream(*key) in remove:
                del self._trackers[key]
        if add or remove:
            log.info("%s resubscribed: +%d -%d streams", self.adapter.name, len(add), len(remove))

    def status(self) -> dict:
        age = (now_ms() - self.last_frame_ms) / 1000 if self.last_frame_ms else None
        return {"exchange": self.adapter.name, "state": self.state, "lastFrameAgeS": age,
                "historyReady": self.history_ready.is_set(), "syncProgress": self.sync_progress,
                "reconnects": self.reconnects, "frames": self.frames, "rejectedStale": self.rejected,
                "streams": len(self._streams), "tracked": sorted(self.tracked),
                "depthSymbols": sorted(self.depth_symbols)}
