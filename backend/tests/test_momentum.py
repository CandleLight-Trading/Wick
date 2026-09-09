from app.models import Candle
from app.store import Store

M = 60_000


async def test_momentum_from_candles_and_from_samples(tmp_path):
    store = Store(tmp_path / "m.db", ring_size=1500)
    await store.open()
    # 121 closed 1m bars: flat for an hour at 100, then a steady climb to 110 over the last hour.
    closes = [100.0] * 61 + [100 + (i + 1) * 10 / 60 for i in range(60)]
    await store.upsert_many([Candle("X", "1m", i * M, c, c, c, c, 1, True) for i, c in enumerate(closes)])
    m = store.momentum("X")
    assert abs(m["vel1h"] - 10.0) < 1e-6 and abs(m["accel1h"] - 10.0) < 1e-6     # was flat, now moving: accelerating
    # Untracked coin: samples only, one per minute, a rise then a stall -> negative acceleration.
    for i in range(130):
        price = 100 + min(i, 65) * 0.1
        store.sample_price("Y", i * M, price)
    m = store.momentum("Y")
    assert m["vel1h"] is not None and m["vel1h"] < 1.0 and m["accel1h"] < 0
    assert store.momentum("Z") == {"vel1h": None, "accel1h": None}
    await store.close()
