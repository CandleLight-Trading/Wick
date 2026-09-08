"""Kraken spot behind the same ExchangeAdapter. Things that differ from Binance:

- Naming. REST (v0) still uses legacy codes: pair keys like XBTUSDT or XXBTZUSD, and
  wsname "XBT/USDT". WebSocket v2 uses "BTC/USDT". As of July 2026 the v2 API renamed
  XBT -> BTC and XDG -> DOGE; older examples online will be wrong. We normalise at this
  boundary: canonical symbols are Binance-style (BTCUSDT), and per-venue names are looked
  up from the AssetPairs response fetched at boot.
- No combined-stream URL. Connect to one fixed URL, then send subscribe messages, one per
  channel (subscribe_on_connect = True).
- OHLC frames carry no "closed" flag. A candle is only known to be final when the next
  interval begins. We mark every live OHLC frame unclosed, so the state machine queues a
  REST refetch of each candle when its successor appears: correct but chatty. That is why
  Kraken runs tickers only by default; enabling klines is a one-line change in main.py.
- Book updates are deltas with a checksum. Only the snapshot is parsed here; maintaining
  a delta book is the upgrade path.
- Public REST has no weight header. Rule of thumb is one request per second per IP.
"""
import json
import logging

from ..models import Candle, Depth, KlineFrame, SymbolInfo, Ticker
from ..rest_client import RestClient
from ..timeutil import interval_to_ms, iso_to_ms, ms_to_s, now_ms, s_to_ms
from .base import ExchangeAdapter

log = logging.getLogger(__name__)

RENAMES = {"XBT": "BTC", "XDG": "DOGE"}
KNOWN_QUOTES = ("USDT", "USDC", "USD", "EUR", "GBP", "BTC", "ETH")
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
MINUTES_INTERVAL = {v: k for k, v in INTERVAL_MINUTES.items()}


def canonical(wsname: str) -> str:
    """'XBT/USDT' or 'BTC/USDT' -> 'BTCUSDT'."""
    base, quote = wsname.split("/")
    return f"{RENAMES.get(base, base)}{RENAMES.get(quote, quote)}"


def split_symbol(symbol: str) -> tuple[str, str]:
    for q in KNOWN_QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    raise ValueError(f"cannot split {symbol}")


class KrakenAdapter(ExchangeAdapter):
    name = "kraken"
    subscribe_on_connect = True

    def __init__(self, rest: RestClient, ws_base: str = "wss://ws.kraken.com/v2"):
        self.rest = rest
        self.ws_base = ws_base
        self._rest_key: dict[str, str] = {}     # canonical -> REST pair key (XBTUSDT)
        self._canonical: dict[str, str] = {}    # REST pair key -> canonical
        self._ws_name: dict[str, str] = {}      # canonical -> v2 name (BTC/USDT), from AssetPairs

    async def _get(self, path: str, params: dict | None = None):
        data = await self.rest.get(path, params)
        if data.get("error"):
            raise RuntimeError(f"kraken {path}: {data['error']}")
        return data["result"]

    def ws_symbol(self, symbol: str) -> str:
        """Prefer the venue's own base/quote split (learned from AssetPairs); guess only
        for symbols we have never seen listed."""
        if symbol in self._ws_name:
            return self._ws_name[symbol]
        base, quote = split_symbol(symbol)
        return f"{base}/{quote}"

    def _key(self, symbol: str) -> str:
        return self._rest_key.get(symbol, symbol)

    # ---- REST -------------------------------------------------------------
    async def fetch_symbols(self) -> list[SymbolInfo]:
        result = await self._get("/0/public/AssetPairs")
        out = []
        for key, p in result.items():
            wsname = p.get("wsname")
            if not wsname or ".d" in key:        # ".d" keys are dark-pool variants
                continue
            base, quote = (RENAMES.get(x, x) for x in wsname.split("/"))
            sym = base + quote
            self._rest_key[sym] = key
            self._canonical[key] = sym
            self._ws_name[sym] = f"{base}/{quote}"
            out.append(SymbolInfo(
                symbol=sym, base=base, quote=quote,
                status="TRADING" if p.get("status", "online") == "online" else p["status"].upper(),
                tick_size=10 ** -int(p.get("pair_decimals", 8)), step_size=10 ** -int(p.get("lot_decimals", 8)),
            ))
        return out

    async def fetch_klines(self, symbol, interval, start_ms, end_ms, limit) -> list[Candle]:
        # Returns at most the last 720 candles; `since` is exclusive and in seconds.
        # Row: [time_s, open, high, low, close, vwap, volume, count] with string prices.
        minutes = INTERVAL_MINUTES[interval]
        result = await self._get("/0/public/OHLC", {"pair": self._key(symbol), "interval": minutes,
                                                     "since": ms_to_s(start_ms) - 1})
        rows = next(v for k, v in result.items() if k != "last")
        step, now = interval_to_ms(interval), now_ms()
        out = []
        for r in rows:
            t = s_to_ms(int(r[0]))
            if t < start_ms or t > end_ms:
                continue
            out.append(Candle(symbol, interval, t, float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                              float(r[6]), closed=t + step <= now))
        return out[:limit]

    async def fetch_tickers(self) -> list[Ticker]:
        # No pair argument returns every pair. Fields: a=[ask..], b=[bid..], c=[last, lot],
        # v=[today, 24h] volume, p=[today, 24h] vwap, h/l=[today, 24h], o=today's open.
        # There is no rolling-24h change; we use today's open (since 00:00 UTC).
        result = await self._get("/0/public/Ticker")
        out = []
        for key, t in result.items():
            sym = self._canonical.get(key)
            if sym is None:
                continue
            last, open_ = float(t["c"][0]), float(t["o"])
            out.append(Ticker(sym, last, (last / open_ - 1) * 100 if open_ else 0.0,
                              float(t["v"][1]) * float(t["p"][1]), float(t["h"][1]), float(t["l"][1]),
                              now_ms(), exchange=self.name))
        return out

    async def fetch_depth(self, symbol, limit) -> Depth:
        result = await self._get("/0/public/Depth", {"pair": self._key(symbol), "count": min(limit, 500)})
        book = next(iter(result.values()))
        return Depth(symbol, [(float(p), float(q)) for p, q, _ in book["bids"]],
                     [(float(p), float(q)) for p, q, _ in book["asks"]], now_ms())

    # ---- WebSocket ---------------------------------------------------------
    def ws_url(self, streams) -> str:
        return self.ws_base

    def kline_stream(self, symbol, interval) -> str:
        return f"ohlc:{self.ws_symbol(symbol)}:{INTERVAL_MINUTES[interval]}"

    def ticker_stream(self, symbol) -> str:
        return f"ticker:{self.ws_symbol(symbol)}"

    def depth_stream(self, symbol) -> str:
        return f"book:{self.ws_symbol(symbol)}"

    def _messages(self, method: str, streams: list[str], req_id: int) -> list[str]:
        # Group our internal stream names by channel (and interval for ohlc): one message each.
        groups: dict[tuple, list[str]] = {}
        for s in streams:
            parts = s.split(":")
            groups.setdefault((parts[0], parts[2] if len(parts) > 2 else None), []).append(parts[1])
        out = []
        for (channel, interval), symbols in groups.items():
            params: dict = {"channel": channel, "symbol": symbols}
            if channel == "ohlc":
                params["interval"] = int(interval)
            if channel == "book":
                params["depth"] = 10
            out.append(json.dumps({"method": method, "params": params, "req_id": req_id}))
        return out

    def subscribe_messages(self, streams, req_id) -> list[str]:
        return self._messages("subscribe", streams, req_id)

    def unsubscribe_messages(self, streams, req_id) -> list[str]:
        return self._messages("unsubscribe", streams, req_id)

    def parse_message(self, raw: str):
        msg = json.loads(raw)
        channel = msg.get("channel")
        if channel == "ticker":
            # {"channel":"ticker","type":"update","data":[{"symbol":"BTC/USDT","bid":..,"ask":..,
            #   "last":..,"volume":..,"vwap":..,"low":..,"high":..,"change_pct":..,"timestamp":ISO}]}
            d = msg["data"][0]
            return Ticker(canonical(d["symbol"]), float(d["last"]), float(d["change_pct"]),
                          float(d["volume"]) * float(d["vwap"]), float(d["high"]), float(d["low"]),
                          iso_to_ms(d["timestamp"]), exchange=self.name)
        if channel == "ohlc":
            # Several candles can arrive in one frame (snapshots); the last is the newest.
            d = msg["data"][-1]
            interval = MINUTES_INTERVAL.get(int(d["interval"]))
            if interval is None:
                return None
            return KlineFrame(
                Candle(canonical(d["symbol"]), interval, iso_to_ms(d["interval_begin"]),
                       float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"]),
                       float(d["volume"]), closed=False),   # Kraken never says "closed"
                event_time=iso_to_ms(d["timestamp"]),
            )
        if channel == "book" and msg.get("type") == "snapshot":
            d = msg["data"][0]
            return Depth(canonical(d["symbol"]), [(float(x["price"]), float(x["qty"])) for x in d["bids"]],
                         [(float(x["price"]), float(x["qty"])) for x in d["asks"]], now_ms())
        # heartbeat, status, subscribe acks, book deltas: nothing to do
        return None
