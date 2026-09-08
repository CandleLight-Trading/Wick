import pytest

from app.adapters.base import ExchangeAdapter
from app.backfill import PAGE, Backfiller, plan_range
from app.models import Candle
from app.store import Store

H = 3_600_000
T0 = 1_700_006_400_000        # an exact hour boundary (472224 * 3600 s)


# ---- plan_range: pure ----------------------------------------------------------
def test_plan_range_first_load_uses_lookback():
    now = T0 + 10 * H + 5
    assert plan_range(None, H, now, 3 * H) == (T0 + 7 * H, T0 + 11 * H)


def test_plan_range_continuous_history_only_wants_forming_candle():
    now = T0 + 10 * H + 5
    assert plan_range(T0 + 9 * H, H, now, 100 * H) == (T0 + 10 * H, T0 + 11 * H)


def test_plan_range_gap_starts_after_last_closed():
    now = T0 + 10 * H + 5
    assert plan_range(T0 + 2 * H, H, now, 100 * H) == (T0 + 3 * H, T0 + 11 * H)


def test_plan_range_lookback_trims_ancient_gap():
    now = T0 + 10 * H + 5
    assert plan_range(T0 - 500 * H, H, now, 3 * H) == (T0 + 7 * H, T0 + 11 * H)


# ---- fetch_range / sync_recent against a fake exchange --------------------------
class FakeExchange(ExchangeAdapter):
    """Serves a synthetic hourly series and enforces Binance's 1000-bar page cap."""
    name = "fake"

    def __init__(self, first: int, last: int):
        self.first, self.last = first, last
        self.requests: list[tuple[int, int, int]] = []

    async def fetch_klines(self, symbol, interval, start_ms, end_ms, limit):
        self.requests.append((start_ms, end_ms, limit))
        assert limit <= PAGE
        out = []
        t = max(start_ms, self.first)
        while t <= min(end_ms, self.last) and len(out) < limit:
            out.append(Candle(symbol, interval, t, 1, 1, 1, 1, 1, closed=t < self.last))
            t += H
        return out

    async def fetch_symbols(self): raise NotImplementedError
    async def fetch_tickers(self): raise NotImplementedError
    async def fetch_depth(self, symbol, limit): raise NotImplementedError
    def ws_url(self, streams): raise NotImplementedError
    def kline_stream(self, symbol, interval): raise NotImplementedError
    def ticker_stream(self, symbol): raise NotImplementedError
    def depth_stream(self, symbol): raise NotImplementedError
    def subscribe_messages(self, streams, req_id): raise NotImplementedError
    def unsubscribe_messages(self, streams, req_id): raise NotImplementedError
    def parse_message(self, raw): raise NotImplementedError


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db", ring_size=1500)
    await s.open()
    yield s
    await s.close()


async def test_fetch_range_paginates_through_the_cap(store):
    n = 2500
    ex = FakeExchange(T0, T0 + (n - 1) * H)
    bf = Backfiller(ex, store)
    got = await bf.fetch_range("X", "1h", T0, T0 + n * H)
    assert got == n
    assert len(ex.requests) == 3                           # 1000 + 1000 + 500
    assert await store.count("X", "1h") == n - 1           # last one is still forming
    assert store.last_closed_open_time("X", "1h") == T0 + (n - 2) * H


async def test_sync_recent_repairs_and_logs_a_gap(store, monkeypatch):
    now = T0 + 100 * H + 5
    monkeypatch.setattr("app.backfill.now_ms", lambda: now)
    ex = FakeExchange(T0, T0 + 100 * H)
    bf = Backfiller(ex, store)
    # Seed continuous history up to hour 60, then pretend we were offline for 40 hours.
    await store.upsert_many([Candle("X", "1h", T0 + i * H, 1, 1, 1, 1, 1, True) for i in range(61)])
    events = []
    bf.on_history_changed = lambda s, i: events.append((s, i))
    got = await bf.sync_recent("X", "1h", 1500 * H)
    assert got == 40                                       # hours 61..100 inclusive (100 is forming)
    assert store.last_closed_open_time("X", "1h") == T0 + 99 * H
    assert len(bf.repairs) == 1 and bf.repairs[0]["candles"] == 40
    assert events == [("X", "1h")]


async def test_sync_recent_with_no_gap_does_not_log_a_repair(store, monkeypatch):
    now = T0 + 100 * H + 5
    monkeypatch.setattr("app.backfill.now_ms", lambda: now)
    ex = FakeExchange(T0, T0 + 100 * H)
    bf = Backfiller(ex, store)
    await store.upsert_many([Candle("X", "1h", T0 + i * H, 1, 1, 1, 1, 1, True) for i in range(100)])
    await bf.sync_recent("X", "1h", 1500 * H)
    assert bf.repairs == []


async def test_live_candle_ahead_of_sync_does_not_hide_the_gap(store, monkeypatch):
    """The bug this guards against: the live feed writes the current candle before the
    connect-time sync runs, so reading the store afterwards looks continuous."""
    now = T0 + 100 * H + 5
    monkeypatch.setattr("app.backfill.now_ms", lambda: now)
    ex = FakeExchange(T0, T0 + 100 * H)
    bf = Backfiller(ex, store)
    await store.upsert_many([Candle("X", "1h", T0 + i * H, 1, 1, 1, 1, 1, True) for i in range(61)])
    snapshot = store.last_closed_open_time("X", "1h")          # taken at connect time
    await store.upsert(Candle("X", "1h", T0 + 99 * H, 1, 1, 1, 1, 1, True))   # live frame lands first
    assert await bf.sync_recent("X", "1h", 1500 * H) == 1      # reading the store now: looks continuous
    assert await bf.sync_recent("X", "1h", 1500 * H, last_closed=snapshot) == 40   # snapshot: real gap
    assert await store.count("X", "1h") == 100


async def test_repair_holes_finds_and_queues_every_gap(store):
    ex = FakeExchange(T0, T0 + 100 * H)
    bf = Backfiller(ex, store)
    times = [i for i in range(100) if not (10 <= i < 15) and i != 50]     # two holes
    await store.upsert_many([Candle("X", "1h", T0 + i * H, 1, 1, 1, 1, 1, True) for i in times])
    assert await bf.repair_holes("X", "1h") == 2
    assert bf._queue.qsize() == 2
    while not bf._queue.empty():
        key = await bf._queue.get()
        await bf.fetch_range("X", "1h", *key[2:])
    assert await store.count("X", "1h") == 100
    assert await bf.repair_holes("X", "1h") == 0


async def test_live_gap_request_is_deduped_and_repaired(store):
    ex = FakeExchange(T0, T0 + 100 * H)
    bf = Backfiller(ex, store)
    bf.request_gap("X", "1h", T0 + 10 * H, T0 + 20 * H)
    bf.request_gap("X", "1h", T0 + 10 * H, T0 + 20 * H)   # duplicate while queued
    assert bf._queue.qsize() == 1
    key = await bf._queue.get()
    n = await bf.fetch_range("X", "1h", *key[2:])
    assert n == 10
