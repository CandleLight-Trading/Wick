"""Store: in-memory ring buffers for fast reads, SQLite for durability.

Reads that serve the frontend come from the ring (a deque of the last RING_SIZE
candles per symbol/interval). Every write also goes to SQLite, so a restart loses
nothing. SQLite is the source of truth; the ring is rebuilt from it whenever a
backfill writes candles that are older than the ring's tail.
"""
import json
import logging
from collections import deque
from pathlib import Path

import aiosqlite

from .models import Candle, Depth, Funding, FundingPoint, Ticker
from .timeutil import now_ms

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
    symbol    TEXT    NOT NULL,
    interval  TEXT    NOT NULL,
    open_time INTEGER NOT NULL,   -- ms since epoch
    o REAL, h REAL, l REAL, c REAL, v REAL,
    closed    INTEGER NOT NULL    -- 0 while forming, 1 once final
);
CREATE UNIQUE INDEX IF NOT EXISTS candles_key ON candles(symbol, interval, open_time);

CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Live out-of-sample log: every condition onset, with forward returns filled in later.
CREATE TABLE IF NOT EXISTS condition_events(
    symbol    TEXT    NOT NULL,
    condition TEXT    NOT NULL,
    open_time INTEGER NOT NULL,
    price     REAL    NOT NULL,
    ret_1h REAL, ret_4h REAL, ret_24h REAL,
    PRIMARY KEY(symbol, condition, open_time)
);

-- Phase 3: model analyses (one row per model call) and the recommendation log.
CREATE TABLE IF NOT EXISTS analyses(
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, time INTEGER NOT NULL, model TEXT,
    stance TEXT, confidence TEXT, horizon_h INTEGER, invalidation REAL, trend TEXT,
    summary TEXT, price REAL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analyses_symbol_time ON analyses(symbol, time);

CREATE TABLE IF NOT EXISTS recommendations(
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, time INTEGER NOT NULL,
    stance TEXT NOT NULL, horizon_h INTEGER NOT NULL, entry REAL NOT NULL, invalidation REAL,
    funding_rate REAL, ret_1h REAL, ret_4h REAL, ret_24h REAL, ret_72h REAL,
    closed_at INTEGER, exit REAL, exit_reason TEXT, pnl_pct REAL
);
CREATE INDEX IF NOT EXISTS recs_open ON recommendations(symbol, source, closed_at);

-- Phase 5. Setups are Wick's opinion about a market situation (no money). Trades are what an
-- account did about a setup. One setup can have trades in several accounts, or none.
CREATE TABLE IF NOT EXISTS setups(
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, detected_at INTEGER NOT NULL, timeframe TEXT NOT NULL,
    horizon_h INTEGER NOT NULL, state TEXT NOT NULL, quality TEXT, bias TEXT, concern TEXT, recommendation TEXT,
    updated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS setups_symbol_state ON setups(symbol, state);

CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, size REAL NOT NULL, daily_loss_pct REAL NOT NULL,
    max_dd_pct REAL NOT NULL, target_pct REAL NOT NULL, created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, setup_id INTEGER NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, state TEXT NOT NULL, planner_version TEXT NOT NULL, risk_profile TEXT NOT NULL,
    plan_entry REAL NOT NULL, trigger_kind TEXT, stop REAL NOT NULL, target REAL, size_usd REAL NOT NULL,
    risk_usd REAL NOT NULL, horizon_h INTEGER NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER,
    opened_at INTEGER, entry REAL, closed_at INTEGER, exit REAL, exit_reason TEXT, pnl_usd REAL, r_multiple REAL,
    status TEXT, status_note TEXT, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_account_state ON trades(account_id, state);

-- Equity per account, once a minute while the desk monitor runs: the Alpaca-style curve.
CREATE TABLE IF NOT EXISTS equity_snapshots(
    account_id INTEGER NOT NULL, time INTEGER NOT NULL, equity REAL NOT NULL,
    PRIMARY KEY(account_id, time)
);
"""

REC_COLS = "id, symbol, source, time, stance, horizon_h, entry, invalidation, funding_rate, ret_1h, ret_4h, ret_24h, ret_72h, closed_at, exit, exit_reason, pnl_pct"


def _rec(r) -> dict:
    return dict(zip(REC_COLS.replace(" ", "").split(","), r))

# Upsert keyed on the unique index: a re-sent closed candle or a backfill overlapping
# live data just overwrites the row. No duplicates are possible.
UPSERT = """
INSERT INTO candles(symbol, interval, open_time, o, h, l, c, v, closed, tb)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
    o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v, closed=excluded.closed,
    tb=COALESCE(excluded.tb, candles.tb)
"""
COLS = "open_time, o, h, l, c, v, closed, tb"


def _row(c: Candle):
    return (c.symbol, c.interval, c.open_time, c.open, c.high, c.low, c.close, c.volume, int(c.closed), c.taker_buy)


def _candle(symbol: str, interval: str, r) -> Candle:
    return Candle(symbol, interval, r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7])


class Store:
    def __init__(self, db_path: Path, ring_size: int):
        self.db_path = db_path
        self.ring_size = ring_size
        self._rings: dict[tuple[str, str], deque[Candle]] = {}
        self._db: aiosqlite.Connection | None = None
        self._dirty = False                       # live candle rows written but not yet committed
        # Latest non-candle state, memory only. Tickers are cheap to refetch; depth is ephemeral.
        self.tickers: dict[str, Ticker] = {}                      # primary exchange (Binance)
        self.other_tickers: dict[str, dict[str, Ticker]] = {}      # exchange -> symbol -> ticker
        self.depth_live: dict[str, Depth] = {}   # top 20 levels from the WS stream
        self.depth_rest: dict[str, Depth] = {}   # 1000 levels from the REST poll
        # Perpetual-futures positioning, memory only (refetched every minute).
        self.funding: dict[str, Funding] = {}
        self.funding_history: dict[str, list[FundingPoint]] = {}
        self.futures_status: dict = {"source": None, "error": None}
        self.dvol: dict[str, float] = {}             # Deribit implied vol index, BTC/ETH, percent
        # Minute-spaced last-price samples from the ticker poll, for the Market momentum columns
        # on coins without local candles. ~2 hours deep. ponytail: memory only, warms up after a restart.
        self.price_samples: dict[str, deque[tuple[int, float]]] = {}
        self.depth_scan: dict[str, Depth] = {}       # depth fetched by the scanner for top movers

    async def open(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._migrate()

    # Numbered migrations. SCHEMA creates missing tables idempotently; anything that changes
    # an existing table lives here, runs once, and is recorded in schema_version.
    MIGRATIONS = [
        (1, "candles.tb: taker-buy volume", "ALTER TABLE candles ADD COLUMN tb REAL"),
        (2, "trades.notes: journal per trade", "ALTER TABLE trades ADD COLUMN notes TEXT"),
    ]

    async def _migrate(self):
        await self._db.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at INTEGER, note TEXT)")
        async with self._db.execute("SELECT MAX(version) FROM schema_version") as cur:
            current = (await cur.fetchone())[0] or 0
        for version, note, sql in self.MIGRATIONS:
            if version <= current:
                continue
            try:
                await self._db.execute(sql)
            except Exception as e:
                if "duplicate column" not in str(e).lower():       # databases created before versioning already have it
                    raise
            await self._db.execute("INSERT INTO schema_version(version, applied_at, note) VALUES(?, ?, ?)", (version, now_ms(), note))
            log.info("migration %d applied: %s", version, note)
        await self._db.commit()

    async def schema_version(self) -> int:
        async with self._db.execute("SELECT MAX(version) FROM schema_version") as cur:
            return (await cur.fetchone())[0] or 0

    async def writable(self) -> bool:
        try:
            await self._db.execute("INSERT INTO settings(key, value) VALUES('healthz', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now_ms()),))
            await self._db.commit()
            return True
        except Exception:
            return False

    async def close(self):
        if self._db:
            await self.flush()
            await self._db.close()

    # ---- candles ------------------------------------------------------------
    def ring(self, symbol: str, interval: str) -> deque[Candle]:
        return self._rings.setdefault((symbol, interval), deque(maxlen=self.ring_size))

    async def upsert(self, c: Candle):
        """Live path: one candle, write-through. The row is written now and committed by
        flush(): with 200+ streams a commit per frame was over a hundred fsyncs a second, and
        every read queued behind them. A crash loses at most a second of forming candles,
        which the next backfill replaces anyway."""
        ring = self.ring(c.symbol, c.interval)
        await self._db.execute(UPSERT, _row(c))
        self._dirty = True
        if ring and ring[-1].open_time == c.open_time:
            ring[-1] = c                                   # in-place update of the forming candle
        elif not ring or c.open_time > ring[-1].open_time:
            ring.append(c)                                 # new candle
        else:
            await self.reload_ring(c.symbol, c.interval)   # older than tail: rebuild from truth

    async def flush(self):
        """Commit whatever the live path wrote. Run every second from main."""
        if self._dirty:
            self._dirty = False
            await self._db.commit()

    async def upsert_many(self, candles: list[Candle]):
        """Backfill path: bulk write, then rebuild affected rings from SQLite."""
        if not candles:
            return
        await self._db.executemany(UPSERT, [_row(c) for c in candles])
        await self._db.commit()
        for symbol, interval in {(c.symbol, c.interval) for c in candles}:
            await self.reload_ring(symbol, interval)

    async def reload_ring(self, symbol: str, interval: str):
        async with self._db.execute(
            f"SELECT {COLS} FROM candles WHERE symbol=? AND interval=? ORDER BY open_time DESC LIMIT ?",
            (symbol, interval, self.ring_size),
        ) as cur:
            rows = await cur.fetchall()
        ring = self.ring(symbol, interval)
        ring.clear()
        ring.extend(_candle(symbol, interval, r) for r in reversed(rows))

    def sample_price(self, symbol: str, t: int, price: float):
        d = self.price_samples.setdefault(symbol, deque(maxlen=130))
        if not d or t - d[-1][0] >= 55_000:
            d.append((t, price))

    def momentum(self, symbol: str) -> dict:
        """First and second derivative of price over one-hour steps, in percent:
        vel1h   = change over the last hour (how fast it is moving now)
        accel1h = that change minus the same change one hour earlier (is it speeding up)
        Tracked coins use closed 1m candles; anything else the poll samples. None until warm."""
        closes = [c.close for c in self.ring(symbol, "1m") if c.closed]
        if len(closes) >= 121:
            now, h1, h2 = closes[-1], closes[-61], closes[-121]
        else:
            d = self.price_samples.get(symbol)
            if not d:
                return {"vel1h": None, "accel1h": None}
            t_now, now = d[-1]
            h1 = next((p for t, p in reversed(d) if t <= t_now - 3_600_000), None)
            h2 = next((p for t, p in reversed(d) if t <= t_now - 7_200_000), None)
            if h1 is None:
                return {"vel1h": None, "accel1h": None}
            vel = (now / h1 - 1) * 100
            return {"vel1h": vel, "accel1h": None if h2 is None else vel - (h1 / h2 - 1) * 100}
        vel = (now / h1 - 1) * 100
        return {"vel1h": vel, "accel1h": vel - (h1 / h2 - 1) * 100}

    def latest(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        ring = self.ring(symbol, interval)
        return list(ring)[-limit:] if limit < len(ring) else list(ring)

    def last_closed_open_time(self, symbol: str, interval: str) -> int | None:
        for c in reversed(self.ring(symbol, interval)):
            if c.closed:
                return c.open_time
        return None

    async def earliest_open_time(self, symbol: str, interval: str) -> int | None:
        async with self._db.execute(
            "SELECT MIN(open_time) FROM candles WHERE symbol=? AND interval=?", (symbol, interval)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def closed_history(self, symbol: str, interval: str, limit: int | None = None) -> list[Candle]:
        """All closed candles oldest-first, or the most recent `limit` of them."""
        sql = f"SELECT {COLS} FROM candles WHERE symbol=? AND interval=? AND closed=1 ORDER BY open_time"
        params: tuple = (symbol, interval)
        if limit:
            sql += " DESC LIMIT ?"
            params += (limit,)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        if limit:
            rows = list(reversed(rows))
        return [_candle(symbol, interval, r) for r in rows]

    async def closed_open_times(self, symbol: str, interval: str) -> list[int]:
        async with self._db.execute(
            "SELECT open_time FROM candles WHERE symbol=? AND interval=? AND closed=1 ORDER BY open_time",
            (symbol, interval),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def count(self, symbol: str, interval: str) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol=? AND interval=? AND closed=1", (symbol, interval)
        ) as cur:
            return (await cur.fetchone())[0]

    # ---- settings ---------------------------------------------------------
    async def get_setting(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        await self._db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._db.commit()

    # ---- condition log ----------------------------------------------------
    async def insert_condition_event(self, symbol: str, condition: str, open_time: int, price: float):
        await self._db.execute(
            "INSERT OR IGNORE INTO condition_events(symbol, condition, open_time, price) VALUES(?, ?, ?, ?)",
            (symbol, condition, open_time, price),
        )
        await self._db.commit()

    async def unresolved_condition_events(self, symbol: str) -> list[tuple]:
        async with self._db.execute(
            "SELECT condition, open_time, price, ret_1h, ret_4h, ret_24h FROM condition_events "
            "WHERE symbol=? AND ret_24h IS NULL", (symbol,)
        ) as cur:
            return await cur.fetchall()

    async def set_condition_return(self, symbol: str, condition: str, open_time: int, column: str, value: float):
        assert column in ("ret_1h", "ret_4h", "ret_24h")
        await self._db.execute(
            f"UPDATE condition_events SET {column}=? WHERE symbol=? AND condition=? AND open_time=?",
            (value, symbol, condition, open_time),
        )
        await self._db.commit()

    # ---- analyses (model judge) ----------------------------------------------
    async def insert_analysis(self, row: dict):
        await self._db.execute(
            "INSERT INTO analyses(symbol, time, model, stance, confidence, horizon_h, invalidation, trend, summary, price, payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row["symbol"], row["time"], row["model"], row["stance"], row["confidence"], row["horizonH"],
             row["invalidation"], row["trend"], row["summary"], row["price"], json.dumps(row)),
        )
        await self._db.commit()

    async def latest_analysis(self, symbol: str) -> dict | None:
        async with self._db.execute("SELECT payload FROM analyses WHERE symbol=? ORDER BY time DESC LIMIT 1", (symbol,)) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def latest_analyses(self) -> list[dict]:
        async with self._db.execute(
            "SELECT payload FROM analyses a WHERE time = (SELECT MAX(time) FROM analyses b WHERE b.symbol = a.symbol)"
        ) as cur:
            return [json.loads(r[0]) for r in await cur.fetchall()]

    async def analyses_today(self) -> int:
        day_start = (now_ms() // 86_400_000) * 86_400_000
        async with self._db.execute("SELECT COUNT(*) FROM analyses WHERE time >= ?", (day_start,)) as cur:
            return (await cur.fetchone())[0]

    # ---- recommendation log / paper positions ----------------------------------
    async def insert_recommendation(self, symbol, source, time, stance, horizon_h, entry, invalidation, funding_rate):
        await self._db.execute(
            "INSERT INTO recommendations(symbol, source, time, stance, horizon_h, entry, invalidation, funding_rate) VALUES(?,?,?,?,?,?,?,?)",
            (symbol, source, time, stance, horizon_h, entry, invalidation, funding_rate))
        await self._db.commit()

    async def open_recommendation(self, symbol, source) -> dict | None:
        async with self._db.execute(
            f"SELECT {REC_COLS} FROM recommendations WHERE symbol=? AND source=? AND closed_at IS NULL ORDER BY time DESC LIMIT 1",
            (symbol, source)) as cur:
            row = await cur.fetchone()
        return _rec(row) if row else None

    async def open_recommendations(self) -> list[dict]:
        async with self._db.execute(f"SELECT {REC_COLS} FROM recommendations WHERE closed_at IS NULL") as cur:
            return [_rec(r) for r in await cur.fetchall()]

    async def close_recommendation(self, rec_id, closed_at, exit_price, reason, pnl):
        await self._db.execute("UPDATE recommendations SET closed_at=?, exit=?, exit_reason=?, pnl_pct=? WHERE id=? AND closed_at IS NULL",
                               (closed_at, exit_price, reason, pnl, rec_id))
        await self._db.commit()

    async def update_recommendation_returns(self, rec_id, rets: dict):
        await self._db.execute("UPDATE recommendations SET ret_1h=?, ret_4h=?, ret_24h=?, ret_72h=? WHERE id=?",
                               (rets["ret_1h"], rets["ret_4h"], rets["ret_24h"], rets["ret_72h"], rec_id))
        await self._db.commit()

    async def recommendations(self, symbol: str | None = None, limit: int = 300) -> list[dict]:
        sql, params = f"SELECT {REC_COLS} FROM recommendations", ()
        if symbol:
            sql, params = sql + " WHERE symbol=?", (symbol,)
        async with self._db.execute(sql + " ORDER BY time DESC LIMIT ?", params + (limit,)) as cur:
            return [_rec(r) for r in await cur.fetchall()]

    async def rec_hit_rates(self, symbol: str | None = None) -> dict:
        """Per source: closed count and share of paper trades that ended net positive."""
        sql = "SELECT source, COUNT(*), SUM(pnl_pct > 0) FROM recommendations WHERE closed_at IS NOT NULL"
        params: tuple = ()
        if symbol:
            sql, params = sql + " AND symbol=?", (symbol,)
        async with self._db.execute(sql + " GROUP BY source", params) as cur:
            return {r[0]: {"n": r[1], "hitRate": (r[2] or 0) / r[1] if r[1] else None} for r in await cur.fetchall()}

    # ---- generic row helpers for the phase 5 tables ----------------------------
    # Rows are returned as dicts; JSON `payload` columns are decoded into a "payload" key.
    async def insert(self, table: str, row: dict) -> int:
        row = {**row, "payload": json.dumps(row.get("payload", {}))} if "payload" in row else row
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = await self._db.execute(f"INSERT INTO {table}({cols}) VALUES({marks})", tuple(row.values()))
        await self._db.commit()
        return cur.lastrowid

    async def update(self, table: str, row_id: int, fields: dict):
        if not fields:
            return
        fields = {**fields, "payload": json.dumps(fields["payload"])} if "payload" in fields else fields
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._db.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))
        await self._db.commit()

    async def rows(self, table: str, where: str = "1=1", params: tuple = (), order: str = "id DESC", limit: int | None = None) -> list[dict]:
        sql = f"SELECT * FROM {table} WHERE {where} ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else "")
        async with self._db.execute(sql, params) as cur:
            names = [d[0] for d in cur.description]
            out = []
            for r in await cur.fetchall():
                d = dict(zip(names, r))
                if "payload" in d and isinstance(d["payload"], str):
                    d["payload"] = json.loads(d["payload"])
                out.append(d)
            return out

    async def row(self, table: str, row_id: int) -> dict | None:
        found = await self.rows(table, "id=?", (row_id,), limit=1)
        return found[0] if found else None

    async def condition_events(self, symbol: str | None, limit: int) -> list[dict]:
        sql = "SELECT symbol, condition, open_time, price, ret_1h, ret_4h, ret_24h FROM condition_events"
        params: tuple = ()
        if symbol:
            sql += " WHERE symbol=?"
            params = (symbol,)
        sql += " ORDER BY open_time DESC LIMIT ?"
        async with self._db.execute(sql, params + (limit,)) as cur:
            rows = await cur.fetchall()
        return [
            {"symbol": r[0], "condition": r[1], "openTime": r[2], "price": r[3],
             "ret1h": r[4], "ret4h": r[5], "ret24h": r[6]}
            for r in rows
        ]
