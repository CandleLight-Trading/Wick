"""Kraken adapter: naming normalisation and raw v2 frame parsing (captured live 2026-09-07)."""
import json

from app.adapters.kraken import KrakenAdapter, canonical, split_symbol
from app.models import Depth, KlineFrame, Ticker


def adapter():
    return KrakenAdapter(rest=None)


async def test_fetch_symbols_uses_the_venue_split_for_odd_quotes():
    class Rest:
        async def get(self, path, params=None):
            return {"error": [], "result": {
                "XBTUSDT": {"altname": "XBTUSDT", "wsname": "XBT/USDT", "pair_decimals": 1, "lot_decimals": 8, "status": "online"},
                "ADAAUD": {"altname": "ADAAUD", "wsname": "ADA/AUD", "pair_decimals": 5, "lot_decimals": 8, "status": "online"},
                "XBTUSD.d": {"altname": "XBTUSD.d", "wsname": "XBT/USD", "pair_decimals": 1, "lot_decimals": 8},
            }}
    a = KrakenAdapter(rest=Rest())
    syms = {s.symbol: s for s in await a.fetch_symbols()}
    assert set(syms) == {"BTCUSDT", "ADAAUD"}            # dark-pool ".d" pair skipped
    assert (syms["ADAAUD"].base, syms["ADAAUD"].quote) == ("ADA", "AUD")
    assert a.ws_symbol("BTCUSDT") == "BTC/USDT" and a._key("BTCUSDT") == "XBTUSDT"


def test_legacy_and_v2_names_both_map_to_canonical():
    assert canonical("XBT/USDT") == "BTCUSDT"
    assert canonical("BTC/USDT") == "BTCUSDT"
    assert canonical("XDG/USD") == "DOGEUSD"
    assert canonical("ETH/USDT") == "ETHUSDT"
    assert split_symbol("DOGEUSDT") == ("DOGE", "USDT")
    assert adapter().ws_symbol("BTCUSDT") == "BTC/USDT"     # v2 wants BTC, never XBT


def test_subscribe_messages_group_by_channel():
    a = adapter()
    msgs = a.subscribe_messages([a.ticker_stream("BTCUSDT"), a.ticker_stream("ETHUSDT"), a.kline_stream("BTCUSDT", "1h")], 7)
    parsed = [json.loads(m) for m in msgs]
    assert len(parsed) == 2
    ticker = next(p for p in parsed if p["params"]["channel"] == "ticker")
    assert sorted(ticker["params"]["symbol"]) == ["BTC/USDT", "ETH/USDT"]
    ohlc = next(p for p in parsed if p["params"]["channel"] == "ohlc")
    assert ohlc["params"] == {"channel": "ohlc", "symbol": ["BTC/USDT"], "interval": 60}


def test_parse_ticker_frame():
    raw = ('{"channel":"ticker","type":"snapshot","data":[{"symbol":"BTC/USDT","bid":79112.0,"bid_qty":0.13,'
           '"ask":79112.1,"ask_qty":0.0085,"last":79102.6,"volume":144.457,"vwap":79709.8,"low":78700.0,'
           '"high":80536.3,"change":-525.8,"change_pct":-0.66,"trades":4461,"timestamp":"2026-09-07T17:10:51.548832Z"}]}')
    t = adapter().parse_message(raw)
    assert isinstance(t, Ticker)
    assert t.symbol == "BTCUSDT" and t.exchange == "kraken"
    assert t.last == 79102.6 and t.change_pct == -0.66
    assert t.event_time == 1788801051548


def test_control_frames_are_ignored():
    a = adapter()
    assert a.parse_message('{"channel":"heartbeat"}') is None
    assert a.parse_message('{"channel":"status","type":"update","data":[{"system":"online"}]}') is None
    assert a.parse_message('{"method":"subscribe","result":{"channel":"ticker"},"success":true}') is None


def test_parse_ohlc_is_never_closed():
    raw = ('{"channel":"ohlc","type":"update","data":[{"symbol":"ETH/USDT","open":2487.0,"high":2490.0,'
           '"low":2486.0,"close":2489.0,"trades":5,"volume":1.5,"vwap":2488.0,"interval_begin":"2026-09-07T17:00:00.000000Z",'
           '"interval":60,"timestamp":"2026-09-07T17:10:51.000000Z"}]}')
    f = adapter().parse_message(raw)
    assert isinstance(f, KlineFrame)
    assert f.candle.symbol == "ETHUSDT" and f.candle.interval == "1h" and f.candle.closed is False
    assert f.candle.open_time == 1788800400000


def test_parse_book_snapshot_only():
    snap = ('{"channel":"book","type":"snapshot","data":[{"symbol":"BTC/USDT","bids":[{"price":79105.9,"qty":0.02}],'
            '"asks":[{"price":79106.0,"qty":0.009}],"checksum":1}]}')
    d = adapter().parse_message(snap)
    assert isinstance(d, Depth) and d.bids == [(79105.9, 0.02)]
    assert adapter().parse_message(snap.replace("snapshot", "update")) is None
