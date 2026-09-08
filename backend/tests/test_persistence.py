"""Phase 6: what happens to trades, candles and the schema across a restart."""
import pytest

from app import retention
from app.models import Candle, Ticker
from app.store import Store
from tests.test_desk import H, M, T0, world  # noqa: F401  (fixture)


async def test_reconciliation_fills_stop_at_the_bar_it_crossed_and_flags_ambiguity(world):
    store, analysis, desk = world
    acct = (await store.rows("accounts"))[0]
    t = await desk.create_trade(None, acct["id"], "standard", None, force=True, symbol="X", side="long")
    stop, target = t["stop"], t["target"]
    assert stop < 100 < target
    # "Downtime": 1m bars land in SQLite while nothing was watching. Bar 3 sweeps through both
    # the target and the stop; bar 5 alone would have looked like a clean stop.
    bars = [Candle("X", "1m", T0 + 300 * H + i * M, 100, 100.2, 99.8, 100, 1, True) for i in range(8)]
    bars[3] = Candle("X", "1m", T0 + 300 * H + 3 * M, 100, target + 1, stop - 1, 100, 1, True)
    bars[5] = Candle("X", "1m", T0 + 300 * H + 5 * M, 100, 100.2, stop - 2, 100, 1, True)
    await store.upsert_many(bars)
    # And a waiting trade whose trigger was touched while offline: it becomes READY, never OPEN.
    w = await desk.create_trade(None, acct["id"], "standard", {"entry": 99.9, "trigger_kind": "pullback"}, force=True, symbol="X", side="long")
    if w["state"] == "open":                       # planner may open at market for a manual trade; force a waiting one
        await store.update("trades", w["id"], {"state": "waiting", "status": "waiting", "opened_at": None, "plan_entry": 99.9, "trigger_kind": "pullback"})

    await desk.reconcile_after_downtime(backfiller=None)
    assert desk.reconciled

    t = await store.row("trades", t["id"])
    assert t["state"] == "closed" and t["exit_reason"] == "stop" and t["exit"] == stop
    assert t["closed_at"] == T0 + 300 * H + 4 * M                  # the close of bar 3, not "now"
    rec = t["payload"]["reconciliation"]
    assert rec["ambiguous"] is True and rec["resolution"] == "1m" and rec["replay"] is True
    w = await store.row("trades", w["id"])
    assert w["state"] == "ready" and "offline" in w["status_note"]


async def test_replay_falls_back_to_coarser_bars_when_1m_is_missing(world):
    store, analysis, desk = world
    acct = (await store.rows("accounts"))[0]
    t = await desk.create_trade(None, acct["id"], "standard", None, force=True, symbol="X", side="long")
    # Only a 5m bar covers the crossing.
    await store.upsert(Candle("X", "5m", T0 + 300 * H, 100, 100.5, t["stop"] - 1, 100, 1, True))
    bars, resolution = await desk._candles_since("X", t["opened_at"])
    assert resolution == "5m" and len(bars) == 1
    await desk.monitor(replay=True)
    t = await store.row("trades", t["id"])
    assert t["state"] == "closed" and t["payload"]["reconciliation"]["resolution"] == "5m"
    assert t["closed_at"] == T0 + 300 * H + 5 * M


async def test_retention_keeps_candles_for_active_trades(world, monkeypatch):
    store, analysis, desk = world
    now = T0 + 300 * H
    monkeypatch.setattr("app.retention.now_ms", lambda: now)
    old = now - 10 * 86_400_000
    await store.upsert_many([Candle(s, "1m", old + i * M, 1, 1, 1, 1, 1, True) for s in ("X", "Y") for i in range(5)])
    acct = (await store.rows("accounts"))[0]
    t = await desk.create_trade(None, acct["id"], "standard", None, force=True, symbol="X", side="long")
    await store.update("trades", t["id"], {"opened_at": old})     # a trade that has been open for ten days
    deleted = await retention.prune(store)
    assert deleted["1m"] == 5                                      # Y pruned, X protected
    async with store._db.execute("SELECT symbol, COUNT(*) FROM candles WHERE interval='1m' AND open_time<? GROUP BY symbol", (now - 7 * 86_400_000,)) as cur:
        assert dict(await cur.fetchall()) == {"X": 5}


async def test_migrations_are_versioned_and_idempotent(tmp_path):
    store = Store(tmp_path / "m.db", ring_size=10)
    await store.open()
    assert await store.schema_version() == len(Store.MIGRATIONS)
    await store.close()
    store = Store(tmp_path / "m.db", ring_size=10)
    await store.open()                                             # second open: nothing to apply, no duplicate-column error
    assert await store.schema_version() == len(Store.MIGRATIONS) and await store.writable()
    await store.close()
