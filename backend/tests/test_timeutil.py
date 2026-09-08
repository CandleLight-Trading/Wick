from app.models import Candle
from app.timeutil import floor_to_interval, interval_to_ms, ms_to_s, s_to_ms


def test_ms_to_s_truncates():
    assert ms_to_s(1_700_000_000_999) == 1_700_000_000
    assert ms_to_s(0) == 0


def test_round_trip():
    assert ms_to_s(s_to_ms(1_700_000_000)) == 1_700_000_000


def test_interval_to_ms():
    assert interval_to_ms("1m") == 60_000
    assert interval_to_ms("15m") == 900_000
    assert interval_to_ms("4h") == 14_400_000
    assert interval_to_ms("1d") == 86_400_000


def test_floor_to_interval_aligns_to_utc_midnight():
    # 2024-01-01T13:37:00Z -> 2024-01-01T00:00:00Z for a 1d interval
    ts = 1_704_116_220_000
    assert floor_to_interval(ts, interval_to_ms("1d")) == 1_704_067_200_000


def test_candle_wire_time_is_seconds():
    c = Candle("BTCUSDT", "1m", 1_700_000_000_000, 1, 2, 0.5, 1.5, 10, True)
    assert c.to_wire()["time"] == 1_700_000_000
