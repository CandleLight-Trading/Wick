"""Desk lifecycle against a fake analysis: setup -> research -> plan -> trade -> monitor -> close."""
import pytest

from app.desk import Desk, quality_of
from app.models import Candle, Ticker
from app.scanner import Features
from app.store import Store

H = 3_600_000
M = 60_000
T0 = 1_700_006_400_000


class FakeJudge:
    status = {"configured": True, "error": None}


class FakeAnalysis:
    def __init__(self, store):
        self.store = store
        self.movers = [{"symbol": "X", "score": 3.0, "features": {"volMultiple": 2.0, "fundingRate": 0.0001, "takerBuyRatio24": 0.6}}]
        self.rules = {"X": {"stance": "long", "trend": "up", "checks": [{"name": f"c{i}", "ok": True, "detail": ""} for i in range(6)]}}
        self.shapes = {"X": {"label": "trend_up", "name": "Clean uptrend", "mfe48h": {"long": 0.10, "short": 0.05}}}
        self.slippage = {}
        self.features = {"X": Features("X", 100.0, 1.0, 0.5, 2.0, 2.0, 55.0, 1, 1, 1, 0.0001, 1e6, None, 0.6, 0.5, 1.0, 0.0, 2.0)}
        self.judge = FakeJudge()
        self.calls = 0

    async def maybe_research(self, symbol, force, user_id=None):
        self.calls += 1
        row = {"symbol": symbol, "time": T0 + 300 * H, "model": "fake", "stance": "long", "confidence": "medium", "userId": user_id,
               "action": "enter", "thesisVerdict": "strengthened", "mainRisk": "extension", "horizonH": 48, "invalidation": 95.0,
               "trend": "trending_up", "summary": "Looks good.", "price": 100.0, "drivers": [], "risks": [], "numbers": [], "citations": [], "usage": {}}
        await self.store.insert_analysis(row)
        return row


@pytest.fixture
async def world(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db", ring_size=1500)
    await store.open()
    # 300 hourly bars with a dip inside the last 48 (swing low 96 -> stop below), then a live price of 100.
    candles = [Candle("X", "1h", T0 + i * H, 100, 100.5, 99.5, 100, 1, True, 0.6) for i in range(300)]
    candles[280] = Candle("X", "1h", T0 + 280 * H, 100, 100.5, 96.0, 100, 1, True, 0.6)
    await store.upsert_many(candles)
    store.tickers["X"] = Ticker("X", 100.0, 1.0, 1e6, 101, 99, 0)
    monkeypatch.setattr("app.desk.now_ms", lambda: T0 + 300 * H)
    analysis = FakeAnalysis(store)
    desk = Desk(store, analysis)
    await desk.ensure_default_account()
    yield store, analysis, desk
    await store.close()


def test_quality_buckets():
    mk = lambda oks: {"checks": [{"name": f"c{i}", "ok": ok, "detail": ""} for i, ok in enumerate(oks)]}
    assert quality_of(mk([True] * 6)) == ("strong", "none")
    assert quality_of(mk([True, True, True, False, True, False]))[0] == "mixed"
    assert quality_of(mk([False] * 5 + [True])) == ("weak", "c0")


async def test_setup_research_plan_trade_close(world):
    store, analysis, desk = world
    await desk.refresh_setups()
    setups = await desk.active_setups()
    s = setups["X"]
    assert s["state"] == "scanned" and s["quality"] == "strong" and s["bias"] == "long"

    # Unresearched: building a trade is refused unless forced.
    acct = (await store.rows("accounts"))[0]
    with pytest.raises(PermissionError):
        await desk.create_trade(s["id"], acct["id"], "standard", None, force=False)
    # ... but "take it anyway" (force) works even before research: Wick advises, the trader decides.
    early = await desk.create_trade(s["id"], acct["id"], "conservative", {"target": None}, force=True)
    assert early["state"] == "open" and early["target"] is None and early["payload"]["wickSaid"] in ("enter", "wait", "pass")
    await desk.close(early["id"], "manual")

    s = await desk.research(s["id"])
    assert s["state"] == "researched" and s["recommendation"] == "enter" and analysis.calls == 1

    p = await desk.plan(s["id"], acct["id"], "standard")
    assert p["plan"]["side"] == "long" and p["plan"]["invalidation"] < 96.0 and p["plan"]["target"] > 100
    assert p["sizing"]["riskUsd"] == pytest.approx(p["account"]["equity"] * 0.0075) and not p["sizing"]["blocked"]

    t = await desk.create_trade(s["id"], acct["id"], "standard", None, force=False)
    assert t["state"] == "open" and t["entry"] == 100.0 and t["planner_version"] == "v1"
    view = await desk.account_view(acct["id"])
    assert view["counts"]["open"] == 1 and view["openRiskUsd"] == pytest.approx(t["risk_usd"])

    # Price rallies to the target: monitor flags it but does not close.
    for i in range(10):
        await store.upsert(Candle("X", "1m", T0 + 300 * H + i * M, 100, 100 + i, 100, 100 + i, 1, True))
    store.tickers["X"] = Ticker("X", 112.0, 1.0, 1e6, 112, 99, 0)
    await desk.monitor()
    t = await store.row("trades", t["id"])
    assert t["state"] == "open" and t["status"] == "target_reached"

    closed = await desk.close(t["id"], "manual")
    assert closed["state"] == "closed" and closed["pnl_usd"] > 0 and closed["r_multiple"] > 1
    view = await desk.account_view(acct["id"])
    assert view["equity"] > 25_000 and view["stats"]["n"] == 2 and view["stats"]["byShape"]["trend_up"]["n"] == 2   # the early forced trade counts too


async def test_clear_research_returns_to_not_run_and_stays_cleared(world):
    store, analysis, desk = world
    await desk.refresh_setups()
    s = list((await desk.active_setups()).values())[0]
    s = await desk.research(s["id"])
    assert s["state"] == "researched" and "research" in s["payload"]
    await desk.clear_research(None, s["id"])
    s = await desk.setup_for_user(await store.row("setups", s["id"]), None)
    assert s["state"] == "scanned" and s["recommendation"] is None and "research" not in s["payload"]
    await desk.refresh_setups()                                   # the cleared analysis must not come back
    assert (await desk.setup_for_user(await store.row("setups", s["id"]), None))["state"] == "scanned"
    assert analysis.calls == 1                                    # clearing spent no model call


async def test_partial_close_books_a_slice_and_shrinks_the_position(world):
    store, analysis, desk = world
    acct = (await store.rows("accounts"))[0]
    t = await desk.create_trade(None, acct["id"], "standard", None, force=True, symbol="X", side="long")
    size, risk = t["size_usd"], t["risk_usd"]
    store.tickers["X"] = Ticker("X", 104.0, 1.0, 1e6, 104, 99, 0)
    pv = await desk.close_preview(t["id"], 0.5)
    assert pv["closeUsd"] == pytest.approx(size / 2) and pv["remainingUsd"] == pytest.approx(size / 2) and pv["realizedUsd"] > 0
    t2 = await desk.close(t["id"], "manual", fraction=0.5)
    assert t2["state"] == "open" and t2["size_usd"] == pytest.approx(size / 2) and t2["risk_usd"] == pytest.approx(risk / 2)
    slices = await store.rows("trades", "state='closed' AND account_id=?", (acct["id"],))
    assert len(slices) == 1 and slices[0]["exit_reason"] == "partial" and slices[0]["pnl_usd"] == pytest.approx(pv["realizedUsd"])
    view = await desk.account_view(acct["id"])
    assert view["counts"]["open"] == 1 and view["stats"]["n"] == 1 and view["openRiskUsd"] == pytest.approx(risk / 2)
    closed = await desk.close(t["id"], "manual")                # the rest, in full
    assert closed["state"] == "closed" and (await desk.account_view(acct["id"]))["counts"]["open"] == 0


async def test_pin_moves_a_watched_coin_into_the_queue(world, monkeypatch):
    store, analysis, desk = world
    monkeypatch.setattr("app.desk.config.TOP_MOVERS", 0)          # nothing is auto-promoted
    analysis.rules["X"] = {"stance": "flat", "trend": "mixed", "checks": [{"name": "c0", "ok": False, "detail": ""}]}
    analysis.movers[0]["symbol"] = "X"
    await desk.refresh_setups()
    assert "X" not in await desk.active_setups()                 # flat and not a top mover: only watched
    s = await desk.pin_setup("X")
    assert s["state"] == "scanned" and s["payload"]["pinned"] and s["quality"] == "weak"
    await desk.refresh_setups()                                   # the scanner keeps it
    assert "X" in await desk.active_setups()
    with pytest.raises(KeyError):
        await desk.pin_setup("NOPE")


async def test_stop_fills_automatically_and_waiting_becomes_ready(world):
    store, analysis, desk = world
    await desk.refresh_setups()
    s = list((await desk.active_setups()).values())[0]
    await desk.research(s["id"])
    acct = (await store.rows("accounts"))[0]
    t = await desk.create_trade(s["id"], acct["id"], "conservative", None, force=False)
    # A 1m candle trades through the stop: the stop order fills at the stop price.
    await store.upsert(Candle("X", "1m", T0 + 300 * H, 100, 100, t["stop"] - 1, 97, 1, True))
    await desk.monitor()
    t = await store.row("trades", t["id"])
    assert t["state"] == "closed" and t["exit_reason"] == "stop" and t["exit"] == t["stop"] and t["pnl_usd"] < 0

    # A waiting trade: pullback trigger at the 20MA, becomes READY when touched, never opens itself.
    # Recommendations now come from the user's own research row, not the shared setup.
    await store._db.execute("UPDATE analyses SET payload = REPLACE(payload, '\"action\": \"enter\"', '\"action\": \"wait\"') WHERE symbol='X'")
    w = await desk.create_trade(s["id"], acct["id"], "standard", None, force=False)
    assert w["state"] == "waiting" and w["trigger_kind"] == "pullback"
    await store.upsert(Candle("X", "1m", T0 + 300 * H + M, 100, 100, w["plan_entry"] - 0.01, 100, 1, True))
    await desk.monitor()
    w = await store.row("trades", w["id"])
    assert w["state"] == "ready"
    opened = await desk.open_now(w["id"])
    assert opened["state"] == "open"
    # "Open at market now" on a WAIT plan fills at the live price, not at the trigger level.
    store.tickers["X"] = Ticker("X", 103.0, 1.0, 1e6, 104, 99, 0)
    now_trade = await desk.create_trade(s["id"], acct["id"], "conservative", {"marketNow": True}, force=False)
    assert now_trade["state"] == "open" and now_trade["entry"] == 103.0


async def test_manual_trade_and_take_it_anyway(world):
    """No setup, no research: a manual short with a nullable target, forced past Wick's PASS.
    Only the account rules can stop it."""
    store, analysis, desk = world
    acct = (await store.rows("accounts"))[0]
    p = await desk.plan_for("X", "short", "enter", acct["id"], "standard")
    assert p["manual"] and p["plan"]["side"] == "short" and p["setup"] is None
    # A manual trade with the target removed: Wick cannot compute R:R, but the trader may still take it.
    t = await desk.create_trade(None, acct["id"], "standard", {"target": None}, force=True, symbol="X", side="short")
    assert t["state"] == "open" and t["setup_id"] == 0 and t["target"] is None and t["payload"]["thesis"] == "Manual trade."
    # A coin with no local history: fallback 5% stop, PASS advisory, force required, still takeable.
    store.tickers["NEW"] = Ticker("NEW", 2.0, 5.0, 1e6, 2.1, 1.9, 0)
    with pytest.raises(PermissionError):
        await desk.create_trade(None, acct["id"], "standard", None, force=False, symbol="NEW", side="long")
    n = await desk.create_trade(None, acct["id"], "standard", None, force=True, symbol="NEW", side="long")
    assert n["state"] == "open" and n["stop"] == pytest.approx(1.9) and n["payload"]["againstAdvice"]
    # The account cap still blocks even when forced.
    with pytest.raises(PermissionError):
        await desk.create_trade(None, acct["id"], "aggressive", {"sizeUsd": 5_000_000}, force=True, symbol="X", side="long")
    await desk.snapshot_equity()
    series = await desk.equity_curve(acct["id"], "1d")
    assert len(series) == 1 and series[0]["equity"] < 25_000          # fees paid on two open positions

    # Sizing reports the account's true ceiling, and it shrinks as open risk is used up.
    assert p["sizing"]["maxNotionalUsd"] > p["sizing"]["notionalUsd"]
    later = await desk.plan_for("X", "long", "enter", acct["id"], "standard")
    assert later["sizing"]["maxRiskUsd"] < p["sizing"]["maxRiskUsd"]


async def test_costs_notes_ledger_and_account_admin(world):
    store, analysis, desk = world
    acct = (await store.rows("accounts"))[0]
    t = await desk.create_trade(None, acct["id"], "standard", None, force=True, symbol="X", side="long")
    await desk.set_notes(t["id"], "felt good about the breakout")
    store.tickers["X"] = Ticker("X", 104.0, 1.0, 1e6, 104, 99, 0)
    closed = await desk.close(t["id"], "manual")
    c = closed["payload"]["costs"]
    assert c["gross"] > 0 and c["fees"] < 0 and c["net"] == pytest.approx(c["gross"] + c["fees"] + c["funding"])
    assert closed["pnl_usd"] == pytest.approx(c["net"])
    csv_text = await desk.ledger_csv(acct["id"])
    assert "felt good about the breakout" in csv_text and "net_pnl_usd" in csv_text.splitlines()[0]
    await desk.rename_account(acct["id"], "  Main desk ")
    assert (await store.row("accounts", acct["id"]))["name"] == "Main desk"
    with pytest.raises(PermissionError):
        await desk.delete_account(acct["id"])                      # never delete the last account
    second = await store.insert("accounts", {"name": "tmp", "size": 1000.0, "daily_loss_pct": 4, "max_dd_pct": 8, "target_pct": 8, "created_at": 0})
    await desk.delete_account(second)
    assert len(await store.rows("accounts")) == 1
