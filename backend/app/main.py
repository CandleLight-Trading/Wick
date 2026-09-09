"""Wires the pieces together:

  Binance WS  -> IngestService -> Store (ring + SQLite) -> Broadcaster -> browser /ws
  Binance REST -> Backfiller  ----------^
  Binance REST -> ticker poll / depth poll -> Store -> /api/market, /api/context
  Kraken WS   -> IngestService (tickers only) -> Store.other_tickers -> cross-exchange divergence
  Futures REST (Binance fapi, else Kraken Futures) -> FuturesPoller -> Store.funding

Phase 1 was Binance only. Phase 2 added the second adapter and the futures poller; the
first two lines did not change, which is what the ExchangeAdapter boundary is for.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import secrets

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, retention
from .auth import Auth, AuthMiddleware
from .adapters.binance import BinanceAdapter
from .adapters.kraken import KrakenAdapter
from .alerts import Alerts
from .analysis import AnalysisService
from .api import router, status_payload
from .deribit import dvol_poll
from .desk import Desk
from .env import is_production, load_env
from .llm import OpenAIJudge
from .backfill import Backfiller
from .broadcaster import Broadcaster
from .conditions import ConditionLog
from .futures import BinanceFutures, FuturesPoller, KrakenFutures
from .ingest import IngestService
from .models import SymbolInfo
from .rest_client import BannedError, RestClient
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


@dataclass
class AppContext:
    store: Store
    rest: RestClient
    adapter: BinanceAdapter
    ingest: IngestService
    backfiller: Backfiller
    broadcaster: Broadcaster
    conditions: ConditionLog
    symbols: list[SymbolInfo]
    futures: FuturesPoller
    analysis: AnalysisService
    alerts: Alerts
    desk: Desk
    secondary: dict[str, IngestService] = field(default_factory=dict)      # exchange -> ticker ingest
    exchange_symbols: dict[str, set[str]] = field(default_factory=dict)    # exchange -> symbols it lists


async def ticker_poll(c: AppContext):
    """Refreshes the whole-exchange 24h ticker cache for the Market screen. Weight 80 per call."""
    while True:
        try:
            for t in await c.adapter.fetch_tickers():
                cur = c.store.tickers.get(t.symbol)
                if cur is None or cur.event_time <= t.event_time:   # never overwrite a fresher WS ticker
                    c.store.tickers[t.symbol] = t
                if t.symbol.endswith("USDT") and t.last:
                    c.store.sample_price(t.symbol, t.event_time, t.last)
        except BannedError:
            return
        except Exception:
            log.exception("ticker poll failed")
        await asyncio.sleep(config.TICKER_POLL_S)


async def depth_poll(c: AppContext):
    """Deep order book for whichever symbols have a Context panel open. Weight 50 per call."""
    while True:
        wanted = set(c.ingest.depth_symbols)
        for sym in list(c.store.depth_rest):
            if sym not in wanted:
                del c.store.depth_rest[sym]
        for sym in wanted:
            try:
                c.store.depth_rest[sym] = await c.adapter.fetch_depth(sym, config.DEPTH_LIMIT)
            except BannedError:
                return
            except Exception:
                log.exception("depth poll failed for %s", sym)
        await asyncio.sleep(config.DEPTH_POLL_S)


async def status_loop(c: AppContext):
    while True:
        c.broadcaster.publish_all(status_payload(c))
        await asyncio.sleep(config.STATUS_BROADCAST_S)


async def start_kraken(store, broadcaster, tracked) -> tuple[IngestService, set[str]] | None:
    """Second exchange, tickers only. If Kraken is unreachable we run without it."""
    rest = RestClient(config.KRAKEN_REST_BASE, soft_limit=10 ** 9, min_interval_s=1.0)
    adapter = KrakenAdapter(rest, config.KRAKEN_WS_BASE)
    try:
        listed = {s.symbol for s in await adapter.fetch_symbols() if s.status == "TRADING"}
    except Exception as e:
        log.warning("kraken unavailable, running without it: %r", e)
        return None
    sink = store.other_tickers.setdefault(adapter.name, {})
    ingest = IngestService(adapter, store, None, broadcaster, sorted(set(tracked) & listed),
                           intervals=[], ticker_sink=sink)
    log.info("kraken lists %d of %d tracked symbols", len(set(tracked) & listed), len(tracked))
    return ingest, listed


async def configure_auth(auth: Auth, store: Store, env: dict):
    """Password from the environment; the cookie-signing secret from the environment or,
    failing that, generated once and kept in the settings table so sessions survive restarts.
    The middleware already holds `auth`, so it is configured in place."""
    password = env.get("WICK_PASSWORD") or None
    prod = is_production(env)
    if prod and not password:
        raise SystemExit("WICK_ENV=production but WICK_PASSWORD is not set; refusing to serve an open desk")
    secret = env.get("WICK_SESSION_SECRET") or await store.get_setting("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        await store.set_setting("session_secret", secret)
    auth.configure(password, secret, secure_cookie=prod)
    log.info("auth %s (%s)", "enabled" if auth.enabled else "disabled: no WICK_PASSWORD", "production" if prod else "development")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(config.DB_PATH, config.RING_SIZE)
    await store.open()
    env = load_env()
    await configure_auth(app.state.auth, store, env)
    rest = RestClient(config.REST_BASE, config.WEIGHT_SOFT_LIMIT)
    adapter = BinanceAdapter(rest, config.WS_BASE)

    saved = await store.get_setting("tracked")
    tracked = json.loads(saved) if saved else list(config.DEFAULT_SYMBOLS)
    symbols = await adapter.fetch_symbols()          # one exchangeInfo call at boot, weight 20
    for sym in tracked:
        for iv in config.INTERVALS:
            await store.reload_ring(sym, iv)         # warm the rings from SQLite before serving

    conditions = ConditionLog(store)

    async def on_close(symbol: str, interval: str):
        if interval == config.CONDITION_INTERVAL:
            await conditions.on_close(symbol)

    # Broadcaster and ingest reference each other; the lambda resolves `ingest` late.
    broadcaster = Broadcaster(config.CLIENT_QUEUE_SIZE, on_depth_wanted=lambda s: ingest.set_depth_symbols(s))
    backfiller = Backfiller(adapter, store, on_history_changed=lambda s, i: broadcaster.publish_all(
        {"type": "backfilled", "symbol": s, "interval": i}))
    ingest = IngestService(adapter, store, backfiller, broadcaster, tracked, on_close=on_close)

    # Futures positioning: Binance fapi first (as specified), Kraken Futures if fapi is
    # geo-blocked (HTTP 451). History is only fetched for symbols with a Context panel open.
    futures = FuturesPoller(
        store,
        [BinanceFutures(RestClient(config.FAPI_BASE, config.WEIGHT_SOFT_LIMIT)),
         KrakenFutures(RestClient(config.KRAKEN_FUTURES_BASE, soft_limit=10 ** 9, min_interval_s=0.5))],
        symbols_fn=lambda: set(ingest.tracked), history_symbols_fn=lambda: set(ingest.depth_symbols),
        poll_s=config.FUTURES_POLL_S, history_poll_s=config.FUNDING_HISTORY_POLL_S,
    )

    kraken = await start_kraken(store, broadcaster, tracked)

    # Phase 3: the Analysis tab. The model judge is optional; without a key the rules judge
    # and the paper scoreboard still run.
    judge = OpenAIJudge(env.get("OPENAI_API_KEY"))
    await judge.verify()
    alerts = Alerts(env)
    analysis = AnalysisService(store, judge, lambda: set(ingest.tracked), kraken[0].adapter.name if kraken else None,
                               adapter=adapter, alerts=alerts)
    async def track(symbol: str):
        """A manual trade on an untracked coin starts tracking it (streams + backfill)."""
        wanted = set(ingest.tracked) | {symbol}
        await store.set_setting("tracked", json.dumps(sorted(wanted)))
        await ingest.set_tracked(wanted)
        for name, other in c.secondary.items():
            await other.set_tracked(wanted & c.exchange_symbols.get(name, set()))

    desk = Desk(store, analysis, alerts, track_fn=track)
    analysis.desk = desk
    await desk.ensure_default_account()

    async def monitor_loop():
        # ALIVE -> READY: wait for continuous history, replay the tape for any trade that
        # lived through the downtime, then the regular minute cadence.
        await ingest.history_ready.wait()
        try:
            await desk.reconcile_after_downtime(backfiller)
        except Exception:
            log.exception("downtime reconciliation failed")
            desk.reconciled = True          # do not hold trade actions hostage to a bug; monitor() still runs each minute
        while True:
            await asyncio.sleep(60)
            try:
                await desk.monitor()
                await desk.snapshot_equity()
            except Exception:
                log.exception("desk monitor failed")

    async def retention_loop():
        await ingest.history_ready.wait()
        while True:
            try:
                await retention.prune(store)
            except Exception:
                log.exception("retention prune failed")
            await asyncio.sleep(config.RETENTION_INTERVAL_S)

    c = AppContext(store, rest, adapter, ingest, backfiller, broadcaster, conditions, symbols, futures, analysis, alerts, desk)
    tasks = [asyncio.create_task(coro) for coro in
             (ingest.run(), backfiller.worker(), ticker_poll(c), depth_poll(c), status_loop(c), futures.run(),
              analysis.run_loop(ready=ingest.history_ready), dvol_poll(store, config.DERIBIT_BASE, config.DVOL_POLL_S),
              monitor_loop(), retention_loop())]
    if kraken:
        k_ingest, k_listed = kraken
        c.secondary[k_ingest.adapter.name] = k_ingest
        c.exchange_symbols[k_ingest.adapter.name] = k_listed
        tasks.append(asyncio.create_task(k_ingest.run()))

    app.state.ctx = c
    log.info("tracking %d symbols, %d known on binance", len(tracked), len(symbols))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await judge.close()
        await alerts.close()
        await rest.close()
        await store.close()


app = FastAPI(title="Wick market data", lifespan=lifespan)
app.state.auth = Auth(None, "placeholder", secure_cookie=False)      # configured in lifespan once the store is open
app.add_middleware(AuthMiddleware, auth=app.state.auth)
app.include_router(router)

# Production: one process serves the built frontend too. Locally Vite serves it and this is a no-op.
if config.FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        target = config.FRONTEND_DIST / path
        if path and target.is_file() and target.resolve().is_relative_to(config.FRONTEND_DIST.resolve()):
            return FileResponse(target)
        return FileResponse(config.FRONTEND_DIST / "index.html")
