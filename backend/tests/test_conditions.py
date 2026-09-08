"""The live condition log: onsets are recorded as bars close, forward returns fill later."""
import pytest

from app.conditions import ConditionLog
from app.models import Candle
from app.store import Store

H = 3_600_000
T0 = 1_700_006_400_000


def bars(closes, start_index=0):
    return [Candle("X", "1h", T0 + (start_index + i) * H, c, c, c, c, 1.0, True) for i, c in enumerate(closes)]


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db", ring_size=1500)
    await s.open()
    yield s
    await s.close()


async def test_onset_is_logged_once_and_returns_fill_in_later(store):
    # 300 flat bars (RSI undefined-high), then one down bar: RSI(14) drops below 30 on the
    # latest closed bar, which is the only moment on_close() records an onset.
    closes = [100.0] * 300 + [99.5]
    await store.upsert_many(bars(closes))
    log = ConditionLog(store)
    await log.on_close("X")
    events = await store.condition_events("X", 100)
    rsi_events = [e for e in events if e["condition"] == "RSI(14) < 30"]
    assert len(rsi_events) == 1
    onset = rsi_events[0]
    assert onset["ret1h"] is None and onset["ret24h"] is None

    # Calling again on the same last bar must not duplicate the onset (still true, not a new onset).
    await log.on_close("X")
    assert len([e for e in await store.condition_events("X", 100) if e["condition"] == "RSI(14) < 30"]) == 1

    # 24 more bars close, each +1%: the +1h/+4h/+24h returns become known.
    last = closes[-1]
    more = [last * 1.01 ** (i + 1) for i in range(24)]
    await store.upsert_many(bars(more, start_index=len(closes)))
    await log.on_close("X")
    resolved = [e for e in await store.condition_events("X", 100) if e["condition"] == "RSI(14) < 30"][0]
    # The onset bar's own close is the entry price; return at +h is close[h] / entry - 1.
    onset_index = (resolved["openTime"] - T0) // H
    all_closes = closes + more
    entry = all_closes[onset_index]
    assert resolved["ret1h"] == pytest.approx(all_closes[onset_index + 1] / entry - 1)
    assert resolved["ret24h"] == pytest.approx(all_closes[onset_index + 24] / entry - 1)
    assert resolved["ret24h"] > 0
