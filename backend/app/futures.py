"""Perpetual-futures positioning data: funding rate, mark price, open interest.

Funding is real positioning information rather than a derived indicator. Perps have no
expiry, so the exchange keeps the perp price tied to spot by making one side pay the
other. Persistently positive funding means longs are crowded and paying to stay in.

Two sources behind one small interface:
  BinanceFutures  fapi.binance.com, as specified. Returns HTTP 451 from restricted
                  locations, in which case the poller falls back to the next source.
  KrakenFutures   futures.kraken.com, public, reachable from more places. USD-margined
                  perps (PF_XBTUSD), so "basis vs Binance USDT spot" includes the USDT/USD
                  rate, which is usually within a few basis points of 1.

Both are normalised to a funding rate PER 8 HOURS as a fraction (0.0001 = 0.01%).
"""
import asyncio
import logging
import statistics
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

import httpx

from .models import Funding, FundingPoint
from .rest_client import RestClient
from .store import Store
from .timeutil import iso_to_ms, now_ms

log = logging.getLogger(__name__)

HISTORY_DAYS = 7


class FuturesSource(ABC):
    name: str

    @abstractmethod
    async def fetch_all(self, symbols: set[str]) -> dict[str, Funding]:
        """Funding + mark + open interest for every requested canonical symbol it can serve."""

    @abstractmethod
    async def fetch_history(self, symbol: str) -> list[FundingPoint]:
        """Funding history for the last HISTORY_DAYS, oldest first, per-8h rates."""


class BinanceFutures(FuturesSource):
    name = "binance-perp"

    def __init__(self, rest: RestClient):
        self.rest = rest

    async def fetch_all(self, symbols):
        # Weight 10 for all symbols. Fields: symbol, markPrice, indexPrice, lastFundingRate
        # (per 8h, as a string), nextFundingTime (ms), time (ms).
        rows = await self.rest.get("/fapi/v1/premiumIndex")
        out = {}
        for r in rows:
            if r["symbol"] not in symbols:
                continue
            out[r["symbol"]] = Funding(
                symbol=r["symbol"], source=self.name,
                mark_price=float(r["markPrice"]), index_price=float(r["indexPrice"]),
                funding_rate=float(r["lastFundingRate"]), next_funding_time=int(r["nextFundingTime"]) or None,
                open_interest=None, time=int(r["time"]),
            )
        # Open interest is one call per symbol (weight 1). {"symbol", "openInterest", "time"}
        for sym in out:
            oi = await self.rest.get("/fapi/v1/openInterest", {"symbol": sym})
            out[sym].open_interest = float(oi["openInterest"])
        return out

    async def fetch_history(self, symbol):
        # Weight 1. 3 fundings per day x 7 days = 21 rows: {"symbol", "fundingTime", "fundingRate", "markPrice"}
        rows = await self.rest.get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": HISTORY_DAYS * 3})
        return [FundingPoint(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]


class KrakenFutures(FuturesSource):
    name = "kraken-perp"
    # Kraken Futures still uses XBT for bitcoin. Perps are USD-margined: BTCUSDT spot -> PF_XBTUSD.
    BASE_RENAMES = {"BTC": "XBT"}

    def __init__(self, rest: RestClient):
        self.rest = rest

    @classmethod
    def perp_symbol(cls, canonical: str) -> str | None:
        if not canonical.endswith("USDT"):
            return None
        base = canonical[:-4]
        return f"PF_{cls.BASE_RENAMES.get(base, base)}USD"

    async def fetch_all(self, symbols):
        # One public call for every contract. Relevant fields per ticker: symbol (PF_XBTUSD),
        # tag ("perpetual"), markPrice, indexPrice, openInterest (base units), fundingRate
        # (ABSOLUTE: quote per contract per HOUR), lastTime (ISO).
        data = await self.rest.get("/derivatives/api/v3/tickers")
        wanted = {self.perp_symbol(s): s for s in symbols if self.perp_symbol(s)}
        out = {}
        for t in data.get("tickers", []):
            canonical = wanted.get(t.get("symbol"))
            if canonical is None or t.get("tag") != "perpetual":
                continue
            mark = t.get("markPrice")
            abs_rate = t.get("fundingRate")
            # Relative hourly rate = absolute / mark; times 8 to express per 8h like Binance.
            rate_8h = abs_rate / mark * 8 if mark and abs_rate is not None else None
            out[canonical] = Funding(
                symbol=canonical, source=self.name, mark_price=mark, index_price=t.get("indexPrice"),
                funding_rate=rate_8h, next_funding_time=None,     # accrues hourly, no discrete event
                open_interest=t.get("openInterest"), time=iso_to_ms(t["lastTime"]) if t.get("lastTime") else now_ms(),
            )
        return out

    async def fetch_history(self, symbol):
        perp = self.perp_symbol(symbol)
        if perp is None:
            return []
        # {"rates": [{"timestamp": ISO, "fundingRate": abs, "relativeFundingRate": hourly fraction}, ...]}
        data = await self.rest.get("/derivatives/api/v4/historicalfundingrates", {"symbol": perp})
        cutoff = now_ms() - HISTORY_DAYS * 86_400_000
        pts = [FundingPoint(iso_to_ms(r["timestamp"]), float(r["relativeFundingRate"]) * 8)
               for r in data.get("rates", []) if r.get("relativeFundingRate") is not None]
        return [p for p in pts if p.time >= cutoff]


def summarize_history(points: list[FundingPoint]) -> dict:
    rates = [p.rate for p in points]
    if not rates:
        return {"n": 0}
    return {
        "n": len(rates),
        "mean": statistics.fmean(rates),
        "positiveShare": sum(1 for r in rates if r > 0) / len(rates),
        "min": min(rates), "max": max(rates),
    }


class FuturesPoller:
    """Refreshes store.funding every minute from the first source that works, and
    funding history every five minutes for whichever symbols have a Context panel open."""

    def __init__(self, store: Store, sources: list[FuturesSource],
                 symbols_fn: Callable[[], set[str]], history_symbols_fn: Callable[[], set[str]],
                 poll_s: int = 60, history_poll_s: int = 300):
        self.store = store
        self.sources = sources
        self.symbols_fn = symbols_fn
        self.history_symbols_fn = history_symbols_fn
        self.poll_s = poll_s
        self.history_poll_s = history_poll_s
        self._idx = 0
        self._history_at: dict[str, int] = {}

    @property
    def source(self) -> FuturesSource | None:
        return self.sources[self._idx] if self._idx < len(self.sources) else None

    def _advance(self, reason: str):
        log.warning("futures source %s unusable (%s); trying the next one", self.source.name, reason)
        self._idx += 1
        self.store.funding.clear()
        self.store.funding_history.clear()
        self._history_at.clear()

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(self.poll_s)

    async def tick(self):
        src = self.source
        if src is None:
            self.store.futures_status = {"source": None, "error": "no futures source reachable from this location"}
            return
        try:
            self.store.funding.update(await src.fetch_all(self.symbols_fn()))
            self.store.futures_status = {"source": src.name, "error": None}
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (451, 403):
                self._advance(f"HTTP {e.response.status_code}: restricted location")
                return await self.tick()
            self.store.futures_status = {"source": src.name, "error": f"HTTP {e.response.status_code}"}
            log.warning("futures poll failed: %s", e)
            return
        except Exception as e:
            self.store.futures_status = {"source": src.name, "error": repr(e)}
            log.warning("futures poll failed: %r", e)
            return
        now = now_ms()
        for sym in self.history_symbols_fn():
            if now - self._history_at.get(sym, 0) < self.history_poll_s * 1000:
                continue
            try:
                self.store.funding_history[sym] = await src.fetch_history(sym)
                self._history_at[sym] = now
            except Exception as e:
                log.warning("funding history for %s failed: %r", sym, e)
