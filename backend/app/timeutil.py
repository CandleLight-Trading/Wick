"""The ONE place where time units are converted.

Binance speaks milliseconds since epoch. lightweight-charts wants seconds.
Every other module stores and passes milliseconds; only the wire format to the
browser goes through ms_to_s(). If you find a `// 1000` anywhere else, that is a bug.
"""
import time

MS_PER_S = 1000


def ms_to_s(ms: int) -> int:
    return ms // MS_PER_S


def s_to_ms(s: int) -> int:
    return s * MS_PER_S


def now_ms() -> int:
    return int(time.time() * MS_PER_S)


def iso_to_ms(iso: str) -> int:
    """Kraken timestamps are ISO-8601 strings like 2026-09-07T17:10:51.548832Z."""
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * MS_PER_S)


_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def interval_to_ms(interval: str) -> int:
    """'1m' -> 60000, '4h' -> 14400000. Binance interval strings are <number><unit>."""
    return int(interval[:-1]) * _UNIT_MS[interval[-1]]


def floor_to_interval(ms: int, interval_ms: int) -> int:
    """Open time of the candle containing `ms`. Binance candles align to epoch, so a
    1d candle opens at 00:00 UTC and a 4h candle at 00:00/04:00/... UTC."""
    return ms - ms % interval_ms
