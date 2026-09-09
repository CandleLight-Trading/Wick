"""All tunables in one place. Nothing here is secret: there are no keys anywhere."""
from pathlib import Path

# Public, unauthenticated Binance market-data hosts. No auth headers are ever sent.
REST_BASE = "https://data-api.binance.vision"
WS_BASE = "wss://data-stream.binance.vision"

# Phase 2 hosts. All public, all unauthenticated.
FAPI_BASE = "https://fapi.binance.com"                  # Binance USDT-margined perps (451 in some regions)
KRAKEN_FUTURES_BASE = "https://futures.kraken.com"      # fallback perps source
KRAKEN_REST_BASE = "https://api.kraken.com"
KRAKEN_WS_BASE = "wss://ws.kraken.com/v2"
FUTURES_POLL_S = 60             # funding/mark/OI for all tracked symbols
FUNDING_HISTORY_POLL_S = 300    # 7-day funding history, per open Context panel

# Symbols tracked on first run. The set is editable from the UI and persisted in SQLite.
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT",
    "UNIUSDT", "ATOMUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
]

# Every tracked symbol subscribes to a kline stream for each of these intervals.
# ponytail: 20 symbols x 6 intervals + 20 tickers = 140 streams on one connection.
# Binance allows 1024 per connection. Past ~150 symbols, derive 5m..1d from 1m instead.
INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]

# In-memory candles kept per (symbol, interval). SQLite keeps everything.
RING_SIZE = 1500

# How far back to backfill, per interval. Intervals not listed get RING_SIZE bars.
# 1h and 1d go deep because the context panel's base rates need years, not weeks.
DAY_MS = 86_400_000
DEEP_HISTORY_MS = {"1h": 730 * DAY_MS, "1d": 730 * DAY_MS}

# REST budget. Binance caps at 1200 weight per minute per IP; we stop at 900.
WEIGHT_SOFT_LIMIT = 900
TICKER_POLL_S = 30      # /ticker/24hr with no symbol is weight 80 -> 160/min
DEPTH_POLL_S = 15       # /depth?limit=1000 is weight 50 -> 200/min, only while a context panel is open
DEPTH_LIMIT = 1000      # 1000 levels reach roughly 0.5% from mid on ETH, less on BTC; the panel shows coverage

# Frontend fan-out: max queued frames per browser client before we drop the oldest.
CLIENT_QUEUE_SIZE = 200
STATUS_BROADCAST_S = 2

# Costs applied to every forward-return figure. Binance spot taker fee is 0.10% per side.
TAKER_FEE_BPS = 10.0
ROUND_TRIP_COST = 2 * TAKER_FEE_BPS / 10_000  # 0.002 as a fraction of price

# Base-rate machinery runs on this interval; horizons are in bars of it.
CONDITION_INTERVAL = "1h"
HORIZONS_BARS = {"1h": 1, "4h": 4, "24h": 24}
MIN_SAMPLE = 30

# The database lives on a persistent volume in production (DATABASE_PATH=/data/wick.sqlite).
# Locally it stays under backend/data. Code is disposable across deploys; this file is not.
import os as _os
DB_PATH = Path(_os.environ.get("DATABASE_PATH") or Path(__file__).resolve().parent.parent / "data" / "market.db")
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# Retention. Raw high-resolution candles are pruned; the exchange remains the recovery source.
# 1h, 4h and 1d are kept forever. Candles overlapping open or pending trades are never pruned.
RETENTION_DAYS = {"1m": 7, "5m": 60, "15m": 180}
EQUITY_THIN_AFTER_DAYS = 7          # older equity snapshots are thinned to one per 5 minutes
RETENTION_INTERVAL_S = 6 * 3600

# ---- Phase 3: Analysis tab -------------------------------------------------
# The key is read from the OPENAI_API_KEY environment variable or backend/.env. Never
# hard-code it here; this file is committed.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
OPENAI_MODEL = "gpt-5.6-luna"        # verified against /v1/models at boot; change if the API rejects it
OPENAI_BASE = "https://api.openai.com"
RESEARCH_CONCURRENCY = 4              # model calls in flight at once; further clicks queue
LLM_RESEARCH_TTL_S = 12 * 3600       # if AUTO_RESEARCH is on: no auto re-research sooner than this
AUTO_RESEARCH = False                # OFF: the model is only called when you click RESEARCH / REFRESH RESEARCH.
                                     # Scanning, rules, shapes and setups are local and free and keep running.
# Research freshness. Staleness is time OR a material market change since the research ran.
RESEARCH_FRESH_S = 2 * 3600
RESEARCH_AGING_S = 6 * 3600
# Cost estimate per call, for the usage counter. Update if the model's pricing changes.
LLM_INPUT_USD_PER_M = 0.20
LLM_OUTPUT_USD_PER_M = 1.20
LLM_SEARCH_USD_PER_CALL = 0.01
SCAN_INTERVAL_S = 900                # mover scan + rules judge + paper-trade resolution
TOP_MOVERS = 5                       # how many scanner picks get a model call per scan
REC_HORIZON_H = 48                   # default holding horizon for a call (prop rules favour 1-3 days)
DAILY_LOSS_BUDGET_PCT = 1.0          # max loss per call as % of account, sizes the suggested notional
PAPER_START_EQUITY = 10_000.0
PAPER_NOTIONAL = 1_000.0             # fixed size per paper position
FUNDING_CROWDED_LONG = 0.0003        # > 0.03%/8h: longs are paying up
FUNDING_CROWDED_SHORT = -0.0001

# ---- Phase 4: flow, liquidity, sizing, alerts, implied vol ------------------------
ACCOUNT_SIZE_USD = 10_000.0          # the prop account you are sizing for; slippage is quoted at this size
PROP_DAILY_LOSS_LIMIT_PCT = 4.0      # typical crypto prop challenge rules; the tracker measures paper P&L against them
PROP_MAX_DRAWDOWN_PCT = 8.0
SLIPPAGE_NOTIONALS = [1_000.0, 10_000.0, 50_000.0]
DERIBIT_BASE = "https://www.deribit.com"
DVOL_POLL_S = 300
# Alert webhooks are read from backend/.env: DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Set to True to see websockets' own DEBUG log, which prints every PING received
# and every PONG sent. This is how you verify ping/pong instead of trusting docs.
LOG_WS_FRAMES = False
