from app.candle_state import Action, CandleTracker
from app.models import Candle

M = 60_000
T0 = 1_700_000_000_000


def frame(open_time, closed=False, close=1.0):
    return Candle("BTCUSDT", "1m", open_time, 1.0, 2.0, 0.5, close, 10.0, closed)


def test_first_frame_is_new_without_gap():
    t = CandleTracker(M)
    d = t.on_frame(frame(T0), 1)
    assert d.action is Action.NEW and d.gap is None


def test_forming_frames_update_in_place_then_close():
    t = CandleTracker(M)
    t.on_frame(frame(T0), 1)
    assert t.on_frame(frame(T0, close=1.1), 2).action is Action.UPDATE
    assert t.on_frame(frame(T0, close=1.2), 3).action is Action.UPDATE
    assert t.on_frame(frame(T0, closed=True), 4).action is Action.CLOSE
    # A duplicate close is harmless (the store upserts).
    assert t.on_frame(frame(T0, closed=True), 5).action is Action.CLOSE


def test_next_candle_after_clean_close_has_no_gap():
    t = CandleTracker(M)
    t.on_frame(frame(T0), 1)
    t.on_frame(frame(T0, closed=True), 2)
    d = t.on_frame(frame(T0 + M), 3)
    assert d.action is Action.NEW and d.gap is None


def test_missed_close_puts_previous_candle_in_gap():
    t = CandleTracker(M)
    t.on_frame(frame(T0), 1)                      # never closed
    d = t.on_frame(frame(T0 + M), 2)
    assert d.action is Action.NEW
    assert d.gap == (T0, T0 + M)                  # refetch the one we never saw close


def test_fifty_candle_jump_covers_whole_span():
    t = CandleTracker(M)
    t.on_frame(frame(T0, closed=True), 1)
    d = t.on_frame(frame(T0 + 50 * M), 2)
    assert d.gap == (T0 + M, T0 + 50 * M)         # 49 missing candles, all of them
    t2 = CandleTracker(M)
    t2.on_frame(frame(T0), 1)                     # unclosed then jump: gap includes T0 itself
    assert t2.on_frame(frame(T0 + 50 * M), 2).gap == (T0, T0 + 50 * M)


def test_older_open_time_is_rejected():
    t = CandleTracker(M)
    t.on_frame(frame(T0 + M), 1)
    assert t.on_frame(frame(T0), 2).action is Action.REJECT_STALE


def test_older_event_time_same_candle_is_rejected():
    t = CandleTracker(M)
    t.on_frame(frame(T0, close=1.2), 10)
    assert t.on_frame(frame(T0, close=1.1), 9).action is Action.REJECT_STALE
    assert t.on_frame(frame(T0, close=1.3), 11).action is Action.UPDATE


def test_cannot_reopen_closed_candle():
    t = CandleTracker(M)
    t.on_frame(frame(T0, closed=True), 1)
    assert t.on_frame(frame(T0, closed=False), 2).action is Action.REJECT_STALE
