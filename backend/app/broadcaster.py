"""Fan-out from the ingest pipeline to browser WebSocket clients.

Each client gets a bounded queue. If a browser is slow (background tab, laptop asleep)
and its queue fills, we drop the OLDEST frame and count it. A chart only ever wants
the newest state of a candle, so a dropped stale update costs nothing; an unbounded
buffer would cost all our memory.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from .models import Candle, Depth, Ticker

log = logging.getLogger(__name__)


class Client:
    def __init__(self, cid: int, ws, maxsize: int):
        self.id = cid
        self.ws = ws
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self.klines: set[tuple[str, str]] = set()   # (symbol, interval)
        self.tickers: set[str] = set()
        self.depth: str | None = None
        self.dropped = 0
        self.user_id: int | None = None          # private events (research, trade ready) go only here

    def offer(self, msg: dict):
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()          # evict the oldest
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(msg)
            self.dropped += 1

    async def sender(self):
        """Drains the queue into the socket. Runs as its own task per client."""
        while True:
            msg = await self.queue.get()
            await self.ws.send_json(msg)


class Broadcaster:
    def __init__(self, queue_size: int, on_depth_wanted: Callable[[set[str]], Awaitable[None]]):
        self.queue_size = queue_size
        self.on_depth_wanted = on_depth_wanted   # tells ingest which depth streams anyone still needs
        self.clients: dict[int, Client] = {}
        self._next_id = 0
        self._depth_wanted: set[str] = set()

    def add(self, ws) -> Client:
        self._next_id += 1
        client = Client(self._next_id, ws, self.queue_size)
        self.clients[client.id] = client
        return client

    async def remove(self, client: Client):
        self.clients.pop(client.id, None)
        await self._recompute_depth()

    async def set_subscription(self, client: Client, klines: set[tuple[str, str]], tickers: set[str], depth: str | None):
        client.klines, client.tickers, client.depth = klines, tickers, depth
        await self._recompute_depth()

    async def _recompute_depth(self):
        wanted = {c.depth for c in self.clients.values() if c.depth}
        if wanted != self._depth_wanted:
            self._depth_wanted = wanted
            await self.on_depth_wanted(wanted)

    # ---- publish -------------------------------------------------------------
    def publish_kline(self, c: Candle):
        key = (c.symbol, c.interval)
        msg = {"type": "kline", "symbol": c.symbol, "interval": c.interval, "candle": c.to_wire()}
        for client in self.clients.values():
            if key in client.klines:
                client.offer(msg)

    def publish_ticker(self, t: Ticker):
        msg = {"type": "ticker", "ticker": t.to_wire()}
        for client in self.clients.values():
            if t.symbol in client.tickers:
                client.offer(msg)

    def publish_depth(self, d: Depth):
        msg = {"type": "depth", "depth": d.to_wire()}
        for client in self.clients.values():
            if client.depth == d.symbol:
                client.offer(msg)

    def publish_all(self, msg: dict):
        for client in self.clients.values():
            client.offer(msg)

    def publish_user(self, user_id: int | None, msg: dict):
        """Private state never fans out: only sockets that authenticated as this user get it."""
        for client in self.clients.values():
            if client.user_id == user_id:
                client.offer(msg)

    def stats(self) -> dict:
        return {"clients": len(self.clients), "droppedFrames": sum(c.dropped for c in self.clients.values())}
