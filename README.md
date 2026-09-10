# Wick

Local, read-only crypto market-data streaming service with a web dashboard. Public
endpoints only (Binance spot, Binance or Kraken perpetuals, Kraken spot): no API keys,
no accounts, no orders, ever.

## Run

```bash
cd backend && uv sync
cd ../frontend && npm install
```

Then either `make dev` (GNU make) or `./dev.ps1` (Windows), or two terminals:

```bash
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
cd frontend && npm run dev
```

Open http://localhost:5173. First start backfills about two minutes of history from
Binance; charts appear within seconds and the Context panel's base rates fill in once the
deep 1h history lands (watch `/api/status` or the backend log).

## Deploy (Railway, one service, SQLite on a volume)

Wick runs as a single process: FastAPI serves the API, the WebSocket and the built
frontend. State is one SQLite file on a persistent volume. Never run more than one replica
or worker: the candle rings, the fan-out and the SQLite writer are per-process.

1. From the region you will host in, run the provider probe first. It checks the official
   endpoints Wick uses (Binance spot, Kraken, Deribit, OpenAI). If Binance spot fails there,
   do not relocate to work around it; make Kraken the primary adapter instead.

```bash
cd backend && uv run python scripts/probe.py
```

2. Create a Railway project from this repo (the Dockerfile and railway.json are picked up),
   attach a volume mounted at /data, and set these variables:

| Variable | Value |
| --- | --- |
| WICK_ENV | production (refuses to start without a password, marks cookies Secure) |
| WICK_PASSWORD | the shared password for the desk |
| WICK_SESSION_SECRET | optional 64 hex chars; generated once and stored in the DB if unset |
| OPENAI_API_KEY | optional; without it the rules judge still runs |
| DATABASE_PATH | /data/market.db (already the Dockerfile default) |

3. Deploy. The health check is `/healthz` (unauthenticated, reports db writability and a
   `ready` flag). The page loads immediately behind the login screen; a banner shows sync
   progress, and research and trade actions unlock once history is continuous and any
   trades left open through the downtime have been reconciled.

4. To move your local history up: copy `backend/data/market.db` onto the volume as
   `/data/market.db` before the first start (Railway volumes accept uploads through a
   one-off shell). Migrations are numbered and run on open.

Log out everywhere (rotate the password afterwards if it leaked):

```bash
curl -X POST -b "wick_session=..." -H "Origin: https://YOUR-HOST" "https://YOUR-HOST/api/logout?everywhere=true"
```

Retention: 1m candles 7 days, 5m 60 days, 15m 180 days, 1h and slower kept; candles for
symbols with open or pending trades are never pruned; equity snapshots older than a week
thin to five-minute spacing. Backups: the DB is a single file; snapshot the volume, or
copy `/data/market.db` while the SQLite WAL is checkpointed (a script is planned).

## Accounts (Clerk)

Without Clerk variables Wick runs on the shared password (or open, in dev). To turn on
accounts, set on the backend: `CLERK_SECRET_KEY`, `CLERK_AUTHORIZED_PARTIES`
(comma-separated origins, default `https://app.candlelit.us,http://localhost:5173`), and
`CLERK_JASON_USER_ID` (the Clerk id that inherits pre-account data and is active by
default). Bake `VITE_CLERK_PUBLISHABLE_KEY` into the frontend at build time (Railway: a
build variable; locally: `frontend/.env.local`). New sign-ups are `invited` until their
`users.access_status` is set to `active`.

## Tests

```bash
cd backend && uv run pytest -q
```

## Layout

```
backend/app/
  config.py        tunables (symbols, intervals, weight limits, fee)
  timeutil.py      the one ms<->s conversion
  models.py        Candle / Ticker / Depth dataclasses
  adapters/        ExchangeAdapter interface + raw Binance implementation
  rest_client.py   weight-aware httpx wrapper (429 backoff, 418 halt)
  candle_state.py  closed/unclosed state machine (pure)
  store.py         ring buffer + SQLite write-through
  backfill.py      gap detection and REST repair
  ingest.py        the Binance WebSocket loop with reconnect
  broadcaster.py   per-client bounded fan-out
  indicators.py    RSI/ATR/MA/vol/correlation/base rates (pure)
  conditions.py    live out-of-sample condition log
  futures.py       funding / mark / open interest (Binance fapi, Kraken Futures fallback)
  adapters/kraken.py  second exchange behind the same interface (tickers, divergence)
  context.py       Context panel payload
  api.py           HTTP + /ws for the frontend
  main.py          wiring
  auth.py          shared-password sessions (cookie, CSRF origin check, throttle)
  retention.py     prune old candles, thin equity snapshots
  scripts/probe.py provider reachability check for a hosting region
frontend/src/
  ws.ts            single WebSocket with ref-counted subscriptions
  screens/         Charts, Market, Context
  components/      CandleChart, SymbolPicker, BaseRateStrip, ...
NOTES.md           design decisions
```

See `NOTES.md` for why things are built the way they are.
