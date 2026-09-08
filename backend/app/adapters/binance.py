"""Raw Binance spot protocol. Read this file to see what ccxt would be hiding.

Casing rule (critical): stream names are lowercase (btcusdt@kline_1m), REST params are
uppercase (symbol=BTCUSDT). We normalise at this boundary and store uppercase everywhere.
"""
import json
import logging

from ..models import Candle, Depth, KlineFrame, SymbolInfo, Ticker
from ..rest_client import RestClient
from ..timeutil import now_ms
from .base import ExchangeAdapter

log = logging.getLogger(__name__)


class BinanceAdapter(ExchangeAdapter):
    name = "binance"

    def __init__(self, rest: RestClient, ws_base: str):
        self.rest = rest
        self.ws_base = ws_base

    # ---- REST -------------------------------------------------------------
    async def fetch_symbols(self) -> list[SymbolInfo]:
        # Weight 20. Response: {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC",
        #   "quoteAsset": "USDT", "status": "TRADING", "filters": [{"filterType": "PRICE_FILTER",
        #   "tickSize": "0.01000000"}, {"filterType": "LOT_SIZE", "stepSize": "0.00001000"}, ...]}]}
        data = await self.rest.get("/api/v3/exchangeInfo")
        out = []
        for s in data["symbols"]:
            filters = {f["filterType"]: f for f in s["filters"]}
            out.append(SymbolInfo(
                symbol=s["symbol"], base=s["baseAsset"], quote=s["quoteAsset"], status=s["status"],
                tick_size=float(filters.get("PRICE_FILTER", {}).get("tickSize", 0)),
                step_size=float(filters.get("LOT_SIZE", {}).get("stepSize", 0)),
            ))
        return out

    async def fetch_klines(self, symbol, interval, start_ms, end_ms, limit) -> list[Candle]:
        # Weight 2. Each row is a positional array:
        # [open_time, open, high, low, close, volume, close_time, quote_volume,
        #  trade_count, taker_buy_base, taker_buy_quote, ignore]  -- prices are STRINGS.
        # endTime is inclusive. The last row may be the still-forming candle.
        rows = await self.rest.get("/api/v3/klines", {
            "symbol": symbol.upper(), "interval": interval,
            "startTime": start_ms, "endTime": end_ms, "limit": min(limit, 1000),
        })
        now = now_ms()
        return [
            Candle(
                symbol=symbol.upper(), interval=interval, open_time=r[0],
                open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
                volume=float(r[5]),
                closed=r[6] < now,   # close_time already in the past -> candle is final
                taker_buy=float(r[9]),
            )
            for r in rows
        ]

    async def fetch_tickers(self) -> list[Ticker]:
        # Weight 80 when no symbol is given (it returns ~2500 symbols). Rolling 24h window.
        rows = await self.rest.get("/api/v3/ticker/24hr")
        return [
            Ticker(
                symbol=r["symbol"], last=float(r["lastPrice"]), change_pct=float(r["priceChangePercent"]),
                quote_volume=float(r["quoteVolume"]), high=float(r["highPrice"]), low=float(r["lowPrice"]),
                event_time=r["closeTime"],
            )
            for r in rows
        ]

    async def fetch_depth(self, symbol, limit) -> Depth:
        # Weight: 5 (limit<=100), 25 (<=500), 50 (<=1000), 250 (5000).
        data = await self.rest.get("/api/v3/depth", {"symbol": symbol.upper(), "limit": limit})
        return Depth(
            symbol=symbol.upper(),
            bids=[(float(p), float(q)) for p, q in data["bids"]],
            asks=[(float(p), float(q)) for p, q in data["asks"]],
            ts=now_ms(),
        )

    # ---- WebSocket ---------------------------------------------------------
    def ws_url(self, streams) -> str:
        # Combined stream: every message is wrapped as {"stream": "<name>", "data": {...}}.
        # We rely on the wrapper because depth frames do not carry the symbol in `data`.
        return f"{self.ws_base}/stream?streams={'/'.join(streams)}"

    def kline_stream(self, symbol, interval) -> str:
        return f"{symbol.lower()}@kline_{interval}"

    def ticker_stream(self, symbol) -> str:
        return f"{symbol.lower()}@ticker"

    def depth_stream(self, symbol) -> str:
        return f"{symbol.lower()}@depth20@100ms"

    def subscribe_messages(self, streams, req_id) -> list[str]:
        # Live (un)subscribe on the open connection, so we never open a socket per symbol.
        return [json.dumps({"method": "SUBSCRIBE", "params": streams, "id": req_id})]

    def unsubscribe_messages(self, streams, req_id) -> list[str]:
        return [json.dumps({"method": "UNSUBSCRIBE", "params": streams, "id": req_id})]

    def parse_message(self, raw: str):
        msg = json.loads(raw)
        if "stream" not in msg:
            # {"result": null, "id": 1} is the ack for SUBSCRIBE/UNSUBSCRIBE.
            return None
        stream, d = msg["stream"], msg["data"]

        if "@kline_" in stream:
            k = d["k"]
            # k.x is "is this kline closed". k.t is open time. Prices are strings.
            return KlineFrame(
                candle=Candle(
                    symbol=k["s"], interval=k["i"], open_time=k["t"],
                    open=float(k["o"]), high=float(k["h"]), low=float(k["l"]), close=float(k["c"]),
                    volume=float(k["v"]), closed=bool(k["x"]),
                    taker_buy=float(k["V"]),      # k.V = taker buy base volume; k.Q is the quote equivalent
                ),
                event_time=d["E"],
            )
        if stream.endswith("@ticker"):
            return Ticker(
                symbol=d["s"], last=float(d["c"]), change_pct=float(d["P"]), quote_volume=float(d["q"]),
                high=float(d["h"]), low=float(d["l"]), event_time=d["E"],
            )
        if "@depth" in stream:
            # Partial book: {"lastUpdateId": ..., "bids": [["price","qty"],...], "asks": [...]}
            # No symbol, no timestamp: both come from the stream name and our clock.
            symbol = stream.split("@")[0].upper()
            return Depth(
                symbol=symbol,
                bids=[(float(p), float(q)) for p, q in d["bids"]],
                asks=[(float(p), float(q)) for p, q in d["asks"]],
                ts=now_ms(),
            )
        log.debug("unhandled stream %s", stream)
        return None
