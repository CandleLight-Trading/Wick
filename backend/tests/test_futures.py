"""Futures sources normalise two very different payloads to one per-8h funding rate,
and the poller falls back when a source is geo-blocked."""
import httpx
import pytest

from app.futures import BinanceFutures, FuturesPoller, KrakenFutures, summarize_history
from app.models import FundingPoint
from app.store import Store


class FakeRest:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        r = self.routes[path]
        if isinstance(r, Exception):
            raise r
        return r(params) if callable(r) else r


def http_error(code):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("blocked", request=req, response=httpx.Response(code, request=req))


async def test_binance_futures_parses_premium_index_and_oi():
    rest = FakeRest({
        "/fapi/v1/premiumIndex": [
            {"symbol": "BTCUSDT", "markPrice": "79100.0", "indexPrice": "79090.0", "lastFundingRate": "0.00010000",
             "nextFundingTime": 1788825600000, "time": 1788801000000},
            {"symbol": "XYZUSDT", "markPrice": "1", "indexPrice": "1", "lastFundingRate": "0", "nextFundingTime": 0, "time": 1},
        ],
        "/fapi/v1/openInterest": lambda p: {"symbol": p["symbol"], "openInterest": "12345.5", "time": 1},
        "/fapi/v1/fundingRate": [{"symbol": "BTCUSDT", "fundingTime": 1788796800000, "fundingRate": "0.00012", "markPrice": "1"}],
    })
    src = BinanceFutures(rest)
    out = await src.fetch_all({"BTCUSDT"})
    assert list(out) == ["BTCUSDT"]
    f = out["BTCUSDT"]
    assert f.funding_rate == 0.0001 and f.open_interest == 12345.5 and f.next_funding_time == 1788825600000
    hist = await src.fetch_history("BTCUSDT")
    assert hist == [FundingPoint(1788796800000, 0.00012)]


async def test_kraken_futures_converts_absolute_hourly_to_relative_8h():
    rest = FakeRest({
        "/derivatives/api/v3/tickers": {"tickers": [
            {"symbol": "PF_XBTUSD", "tag": "perpetual", "markPrice": 80000.0, "indexPrice": 79990.0,
             "openInterest": 2000.0, "fundingRate": 0.8, "lastTime": "2026-09-07T17:12:00.000000Z"},
            {"symbol": "FI_XBTUSD_260925", "tag": "quarterly", "markPrice": 80100.0},
        ]},
        "/derivatives/api/v4/historicalfundingrates": {"rates": [
            {"timestamp": "2026-09-07T10:00:00Z", "fundingRate": 0.8, "relativeFundingRate": 0.00001},
        ]},
    })
    src = KrakenFutures(rest)
    assert KrakenFutures.perp_symbol("BTCUSDT") == "PF_XBTUSD"
    assert KrakenFutures.perp_symbol("DOGEUSDT") == "PF_DOGEUSD"
    out = await src.fetch_all({"BTCUSDT", "ETHUSDT"})
    f = out["BTCUSDT"]
    assert f.funding_rate == pytest.approx(0.8 / 80000 * 8)     # absolute USD/hour -> fraction per 8h
    assert f.next_funding_time is None and f.open_interest == 2000.0
    assert "ETHUSDT" not in out                                 # not in the payload -> absent, not fabricated


async def test_poller_falls_back_on_451(tmp_path):
    store = Store(tmp_path / "t.db", 10)
    await store.open()
    blocked = BinanceFutures(FakeRest({"/fapi/v1/premiumIndex": http_error(451)}))
    kraken = KrakenFutures(FakeRest({"/derivatives/api/v3/tickers": {"tickers": [
        {"symbol": "PF_ETHUSD", "tag": "perpetual", "markPrice": 2500.0, "indexPrice": 2500.0,
         "openInterest": 1.0, "fundingRate": 0.0, "lastTime": "2026-09-07T17:12:00.000000Z"}]}}))
    poller = FuturesPoller(store, [blocked, kraken], lambda: {"ETHUSDT"}, lambda: set())
    await poller.tick()
    assert store.futures_status == {"source": "kraken-perp", "error": None}
    assert store.funding["ETHUSDT"].source == "kraken-perp"
    await store.close()


def test_summarize_history():
    s = summarize_history([FundingPoint(1, 0.0001), FundingPoint(2, -0.0001), FundingPoint(3, 0.0003)])
    assert s["n"] == 3 and s["positiveShare"] == pytest.approx(2 / 3) and s["max"] == 0.0003
    assert summarize_history([]) == {"n": 0}
