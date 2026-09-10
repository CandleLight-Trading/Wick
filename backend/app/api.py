"""HTTP + WebSocket API for the frontend. Every response is served from memory or
SQLite. No handler here ever calls Binance: that is the rule that keeps a curious
browser from getting the whole machine IP-banned."""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .config import INTERVALS, PAPER_NOTIONAL, PAPER_START_EQUITY
from .context import build_context
from .paper import scoreboard
from .structure import read_structure
from .timeutil import ms_to_s, now_ms

log = logging.getLogger(__name__)
router = APIRouter()


def ctx(request: Request):
    return request.app.state.ctx


def ready_ctx(request: Request):
    """Fresh research and trade actions wait until stored history is continuous and any
    trades left open through downtime have been reconciled. Reading is always allowed."""
    c = ctx(request)
    if not (c.ingest.history_ready.is_set() and c.desk.reconciled):
        raise HTTPException(503, "Wick is still syncing market history. Try again in a moment.")
    return c


class LoginBody(BaseModel):
    password: str


def _client_addr(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?")


@router.get("/healthz")
async def healthz(request: Request):
    """Unauthenticated and minimal: is the process up and the database writable?"""
    c = ctx(request)
    ok = await c.store.writable()
    body = {"status": "ok" if ok else "degraded", "ready": c.ingest.history_ready.is_set() and c.desk.reconciled}
    return Response(content=json.dumps(body), media_type="application/json", status_code=200 if ok else 503)


@router.get("/api/auth")
def auth_state(request: Request):
    a = request.app.state.auth
    return {"enabled": a.enabled, "loggedIn": a.session_ok(request)}


@router.post("/api/login")
def login(body: LoginBody, request: Request):
    a = request.app.state.auth
    if not a.enabled:
        return {"ok": True}
    addr = _client_addr(request)
    if a.throttled(addr):
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
    if not a.check_password(body.password):
        a.record_failure(addr)
        raise HTTPException(401, "Wrong password.")
    r = Response(content=json.dumps({"ok": True}), media_type="application/json")
    a.set_cookie(r, a.issue())
    return r


@router.post("/api/logout")
def logout(request: Request, everywhere: bool = False):
    a = request.app.state.auth
    if everywhere:
        a.revoke_all()
    r = Response(content=json.dumps({"ok": True}), media_type="application/json")
    a.clear_cookie(r)
    return r


class TrackedBody(BaseModel):
    symbols: list[str]


@router.get("/api/symbols")
def symbols(request: Request):
    c = ctx(request)
    tracked = c.ingest.tracked
    return [
        {"symbol": s.symbol, "base": s.base, "quote": s.quote, "tracked": s.symbol in tracked}
        for s in c.symbols if s.status == "TRADING"
    ]


@router.get("/api/tracked")
def tracked(request: Request):
    return sorted(ctx(request).ingest.tracked)


@router.put("/api/tracked")
async def set_tracked(body: TrackedBody, request: Request):
    c = ctx(request)
    known = {s.symbol for s in c.symbols}
    wanted = {s.upper() for s in body.symbols}
    bad = wanted - known
    if bad:
        raise HTTPException(400, f"unknown symbols: {sorted(bad)}")
    if not wanted:
        raise HTTPException(400, "track at least one symbol")
    await c.store.set_setting("tracked", json.dumps(sorted(wanted)))
    await c.ingest.set_tracked(wanted)
    for name, other in c.secondary.items():
        await other.set_tracked(wanted & c.exchange_symbols.get(name, set()))
    return sorted(wanted)


@router.get("/api/candles")
def candles(request: Request, symbol: str, interval: str = "1m", limit: int = 500):
    if interval not in INTERVALS:
        raise HTTPException(400, f"interval must be one of {INTERVALS}")
    c = ctx(request)
    symbol = symbol.upper()
    return {"symbol": symbol, "interval": interval, "tracked": symbol in c.ingest.tracked,
            "candles": [x.to_wire() for x in c.store.latest(symbol, interval, limit)]}


@router.get("/api/structure")
def structure(request: Request, symbol: str, interval: str = "1h"):
    """What a trader's eye picks out of the chart, found deterministically: swings, levels,
    structure, events; plus the 1h shape and playbook the Analysis tab already uses, so the
    chart guides and the desk never disagree."""
    if interval not in INTERVALS:
        raise HTTPException(400, f"interval must be one of {INTERVALS}")
    c = ctx(request)
    symbol = symbol.upper()
    s = read_structure(c.store.latest(symbol, interval, 500))
    shape = c.analysis.shapes.get(symbol)
    rules = c.analysis.rules.get(symbol) or {}
    f = c.analysis.features.get(symbol)
    return {**s, "symbol": symbol, "interval": interval,
            "shape": {"label": shape["label"], "name": shape["name"], "description": shape["description"]} if shape else None,
            "playbook": rules.get("playbook"), "riskCharacter": rules.get("riskCharacter"), "stance": rules.get("stance"),
            "moveAtr": f.moveAtr if f else None, "volMultiple": f.volMultiple if f else None, "atrDailyPct": f.atrDailyPct if f else None}


@router.get("/api/market")
def market(request: Request, quote: str = "USDT"):
    """Every pair with the given quote from the 24h ticker cache. Tracked pairs also get a
    sparkline of the last 24 closed hourly closes. The volume filter is applied client-side
    so the slider is instant."""
    c = ctx(request)
    rows = []
    for t in c.store.tickers.values():
        if not t.symbol.endswith(quote):
            continue
        is_tracked = t.symbol in c.ingest.tracked
        spark = [x.close for x in c.store.latest(t.symbol, "1h", 25)] if is_tracked else None
        others = {name: tk[t.symbol].to_wire() for name, tk in c.store.other_tickers.items() if t.symbol in tk}
        rows.append({**t.to_wire(), "tracked": is_tracked, "sparkline": spark, "others": others, **c.store.momentum(t.symbol)})
    return {"quote": quote, "rows": rows, "exchanges": sorted(c.store.other_tickers)}


@router.get("/api/context")
async def context(request: Request, symbol: str):
    c = ctx(request)
    symbol = symbol.upper()
    if symbol not in c.ingest.tracked:
        raise HTTPException(404, f"{symbol} is not tracked; add it from the symbol picker first")
    try:
        return await build_context(c.store, symbol, len(c.ingest.tracked))
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/api/condition_log")
async def condition_log(request: Request, symbol: str | None = None, limit: int = 500):
    rows = await ctx(request).store.condition_events(symbol.upper() if symbol else None, limit)
    for r in rows:
        r["time"] = ms_to_s(r.pop("openTime"))
    return rows


@router.get("/api/analysis")
async def analysis(request: Request):
    return await ctx(request).analysis.payload()


@router.post("/api/analysis/run")
async def analysis_run(request: Request, symbol: str):
    """On-demand research for one symbol: starts a job and returns at once."""
    c = ready_ctx(request)
    symbol = symbol.upper()
    if symbol not in c.ingest.tracked:
        raise HTTPException(404, f"{symbol} is not tracked")
    if not c.analysis.judge.status.get("configured"):
        raise HTTPException(409, c.analysis.judge.status.get("error") or "model judge unavailable")
    if symbol not in c.analysis.features:
        await c.analysis.scan(only={symbol})
    return c.analysis.start_research(symbol)


@router.get("/api/paper")
async def paper(request: Request, symbol: str | None = None):
    c = ctx(request)
    recs = await c.store.recommendations(symbol.upper() if symbol else None)
    closed = [r for r in recs if r["closed_at"] is not None]
    board = {src: scoreboard([r for r in closed if r["source"] == src]) for src in ("rules", "model")}
    for r in recs:
        for k in ("time", "closed_at"):
            if r[k] is not None:
                r[k] = ms_to_s(r[k])
    return {"open": [r for r in recs if r["closed_at"] is None], "closed": closed[:100],
            "scoreboard": board, "notional": PAPER_NOTIONAL, "startEquity": PAPER_START_EQUITY}


# ---- Phase 5: setups, accounts, trades --------------------------------------------
class AccountBody(BaseModel):
    name: str
    size: float = 25_000.0
    daily_loss_pct: float = 4.0
    max_dd_pct: float = 8.0
    target_pct: float = 8.0


class TradeBody(BaseModel):
    setup_id: int | None = None       # guided: from a setup
    symbol: str | None = None         # manual: symbol + side
    side: str | None = None
    account_id: int
    risk_profile: str = "standard"
    overrides: dict | None = None
    force: bool = False


def _perm(e: Exception):
    if isinstance(e, PermissionError):
        return HTTPException(409, str(e))
    if isinstance(e, KeyError):
        return HTTPException(404, str(e))
    return HTTPException(400, str(e))


@router.post("/api/setups/{setup_id}/research")
async def setup_research(setup_id: int, request: Request):
    """Starts a research job and returns immediately. The page learns the result over /ws."""
    c = ready_ctx(request)
    if not c.analysis.judge.status.get("configured"):
        raise HTTPException(409, c.analysis.judge.status.get("error") or "model judge unavailable")
    try:
        return await c.desk.research(setup_id)
    except Exception as e:
        raise _perm(e)


@router.post("/api/setups/{setup_id}/pass")
async def setup_pass(setup_id: int, request: Request):
    await ctx(request).desk.pass_setup(setup_id)
    return {"ok": True}


@router.post("/api/setups/pin")
async def setup_pin(request: Request, symbol: str):
    try:
        return await ctx(request).desk.pin_setup(symbol.upper())
    except Exception as e:
        raise _perm(e)


@router.post("/api/setups/{setup_id}/clear_research")
async def setup_clear_research(setup_id: int, request: Request):
    await ctx(request).desk.clear_research(setup_id)
    return {"ok": True}


@router.post("/api/analysis/clear_research")
async def analysis_clear_research(request: Request):
    await ctx(request).desk.clear_research(None)
    return {"ok": True}


@router.post("/api/analysis/reset_workspace")
async def analysis_reset_workspace(request: Request):
    await ctx(request).desk.reset_workspace()
    return {"ok": True}


@router.get("/api/plan")
async def plan(request: Request, account_id: int, setup_id: int | None = None, symbol: str | None = None,
               side: str = "long", risk_profile: str = "standard"):
    c = ctx(request)
    d = c.desk
    try:
        if setup_id:
            return await d.plan(setup_id, account_id, risk_profile)
        if not symbol:
            raise KeyError("setup_id or symbol required")
        symbol = symbol.upper()
        if symbol not in c.ingest.tracked and d.track_fn:
            await d.track_fn(symbol)              # manual ticket on an untracked coin: start the data flow now
        return await d.plan_for(symbol, side, "enter", account_id, risk_profile)
    except Exception as e:
        raise _perm(e)


@router.post("/api/analysis/scan")
async def analysis_scan(request: Request, symbol: str | None = None):
    """ANALYZE from Market or Charts: track the symbol if needed and rescan now."""
    c = ctx(request)
    if symbol:
        symbol = symbol.upper()
        if symbol in c.analysis.features:
            await c.analysis.scan(only={symbol})
            return {"ok": True, "lastScan": ms_to_s(c.analysis.last_scan_ms), "warming": False}
        # New coin: track it, wait for its shallow backfill, scan just it. All in the background so
        # the page can show a "warming" card at once instead of a spinner on a button.
        c.analysis.warming.add(symbol)

        async def warm():
            try:
                if symbol not in c.ingest.tracked and c.desk.track_fn:
                    await c.desk.track_fn(symbol)
                await c.ingest.wait_synced(symbol, timeout=60)
                await c.analysis.scan(only={symbol})
            except Exception:
                log.exception("warmup for %s failed", symbol)
            finally:
                c.analysis.warming.discard(symbol)
                if c.analysis.broadcaster:
                    c.analysis.broadcaster.publish_all({"type": "scan", "lastScan": ms_to_s(c.analysis.last_scan_ms), "symbol": symbol})
        asyncio.create_task(warm())
        return {"ok": True, "lastScan": ms_to_s(c.analysis.last_scan_ms), "warming": True}
    await c.analysis.scan()
    return {"ok": True, "lastScan": ms_to_s(c.analysis.last_scan_ms), "warming": False}


@router.get("/api/equity")
async def equity(request: Request, account_id: int, range: str = "1w"):
    return await ctx(request).desk.equity_curve(account_id, range)


@router.get("/api/accounts")
async def accounts(request: Request):
    c = ctx(request)
    return [await c.desk.account_view(a["id"]) for a in await c.store.rows("accounts", order="id ASC")]


@router.post("/api/accounts")
async def create_account(body: AccountBody, request: Request):
    c = ctx(request)
    aid = await c.store.insert("accounts", {**body.model_dump(), "created_at": now_ms()})
    return await c.desk.account_view(aid)


class NameBody(BaseModel):
    name: str


class NotesBody(BaseModel):
    notes: str


@router.patch("/api/accounts/{account_id}")
async def rename_account(account_id: int, body: NameBody, request: Request):
    c = ctx(request)
    await c.desk.rename_account(account_id, body.name)
    return await c.desk.account_view(account_id)


@router.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, request: Request):
    try:
        await ctx(request).desk.delete_account(account_id)
    except Exception as e:
        raise _perm(e)
    return {"ok": True}


@router.get("/api/accounts/{account_id}/ledger.csv")
async def ledger(account_id: int, request: Request):
    c = ctx(request)
    acct = await c.store.row("accounts", account_id)
    if not acct:
        raise HTTPException(404, "account not found")
    name = "".join(ch if ch.isalnum() else "_" for ch in acct["name"]).strip("_") or "account"
    return Response(await c.desk.ledger_csv(account_id), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="wick_ledger_{name}.csv"'})


@router.patch("/api/trades/{trade_id}")
async def trade_notes(trade_id: int, body: NotesBody, request: Request):
    c = ctx(request)
    await c.desk.set_notes(trade_id, body.notes)
    return await c.store.row("trades", trade_id)


@router.post("/api/trades")
async def create_trade(body: TradeBody, request: Request):
    try:
        return await ready_ctx(request).desk.create_trade(body.setup_id, body.account_id, body.risk_profile, body.overrides, body.force,
                                                    symbol=body.symbol, side=body.side)
    except Exception as e:
        raise _perm(e)


class CloseBody(BaseModel):
    fraction: float = 1.0


@router.get("/api/trades/{trade_id}/close_preview")
async def close_preview(trade_id: int, request: Request, fraction: float = 1.0):
    try:
        return await ctx(request).desk.close_preview(trade_id, fraction)
    except Exception as e:
        raise _perm(e)


@router.post("/api/trades/{trade_id}/close")
async def trade_close(trade_id: int, request: Request, body: CloseBody | None = None):
    """Close all or part of a position. The UI shows a preview and asks for confirmation first."""
    try:
        return await ctx(request).desk.close(trade_id, "manual", fraction=(body.fraction if body else 1.0))
    except Exception as e:
        raise _perm(e)


@router.post("/api/trades/{trade_id}/research")
async def trade_research(trade_id: int, request: Request):
    try:
        return await ready_ctx(request).desk.research_position(trade_id)
    except Exception as e:
        raise _perm(e)


@router.post("/api/trades/{trade_id}/{action}")
async def trade_action(trade_id: int, action: str, request: Request):
    d = ready_ctx(request).desk
    try:
        if action == "open":
            return await d.open_now(trade_id)
        if action == "cancel":
            await d.cancel(trade_id)
            return {"ok": True}
        raise HTTPException(404, "unknown action")
    except HTTPException:
        raise
    except Exception as e:
        raise _perm(e)


@router.get("/api/prop")
async def prop(request: Request, account_id: int, range: str = "all"):
    d = ctx(request).desk
    acct = await d.account_view(account_id)
    if not acct:
        raise HTTPException(404, "account not found")
    return {"account": acct, **(await d.trades_view(account_id)), "judges": await d.judges_tally(),
            "equity": await d.equity_curve(account_id, range)}


def status_payload(c) -> dict:
    return {"type": "status", "ready": c.ingest.history_ready.is_set() and c.desk.reconciled,
            "ingest": c.ingest.status(), "rest": c.rest.status(),
            "broadcast": c.broadcaster.stats(), "repairs": c.backfiller.repairs[-20:],
            "exchanges": {name: ing.status() for name, ing in c.secondary.items()},
            "futures": c.store.futures_status, "alerts": c.alerts.status(), "dvol": c.store.dvol}


@router.get("/api/status")
def status(request: Request):
    return status_payload(ctx(request))


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """Protocol: client sends {"op": "subscribe", "klines": [["BTCUSDT","1m"], ...],
    "tickers": ["BTCUSDT"], "depth": "BTCUSDT" | null}. Each message REPLACES the client's
    subscription set. Server pushes kline / ticker / depth / status / backfilled messages."""
    c = ws.app.state.ctx
    auth = ws.app.state.auth
    if auth.enabled and not (auth.session_ok(ws) and auth.same_origin(ws.headers, ws.headers.get("host", ""))):
        await ws.accept()               # accept then close, so the browser sees our code instead of a bare 403
        await ws.close(code=4401, reason="login required")
        return
    await ws.accept()
    client = c.broadcaster.add(ws)
    sender = asyncio.create_task(client.sender())
    client.offer(status_payload(c))
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("op") == "subscribe":
                klines = {(s.upper(), i) for s, i in msg.get("klines", []) if i in INTERVALS}
                tickers = {s.upper() for s in msg.get("tickers", [])}
                depth = (msg.get("depth") or "").upper() or None
                await c.broadcaster.set_subscription(client, klines, tickers, depth)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws client %d errored", client.id)
    finally:
        sender.cancel()
        await c.broadcaster.remove(client)
