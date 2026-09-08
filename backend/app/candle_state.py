"""The closed/unclosed candle state machine. Pure: no I/O, no clock, fully testable.

Why this exists: a kline stream sends the SAME candle over and over while it forms
(k.x == false), then once more with k.x == true when it closes. If you append every
message you get dozens of copies of each candle and every indicator built on top is
silently wrong. So each (symbol, interval) has a tracker that decides, per frame:

  UPDATE       same open_time, still forming   -> overwrite the in-progress candle
  CLOSE        same open_time, x == true        -> overwrite and mark final
  NEW          later open_time                  -> a new candle has started
  REJECT_STALE earlier open_time / older event  -> ignore, it is out of order

Staleness is judged against the kline's own open_time and the exchange's event time,
never against wall clock. Wall clock only drives the UI's "stale" indicator.

NEW also reports a gap: the range of candles we did NOT see between the previous
candle and this one. One missed close (previous never got x == true) and fifty missed
candles (long stall with no disconnect) produce the same kind of answer: a half-open
range [gap_start, gap_end) of open_times that the backfiller fetches from REST.
"""
from dataclasses import dataclass
from enum import Enum

from .models import Candle


class Action(Enum):
    UPDATE = "update"
    CLOSE = "close"
    NEW = "new"
    REJECT_STALE = "reject_stale"


@dataclass(slots=True)
class Decision:
    action: Action
    candle: Candle
    gap: tuple[int, int] | None = None   # [start_open_time, end_open_time) missing, or None


class CandleTracker:
    def __init__(self, interval_ms: int):
        self.interval_ms = interval_ms
        self.open_time: int | None = None
        self.closed = False
        self.event_time = 0

    def on_frame(self, candle: Candle, event_time: int) -> Decision:
        if self.open_time is None:
            # First frame after (re)connect. No gap computed here: the connect-time
            # backfill compares against the store, which knows more than we do.
            self._set(candle, event_time)
            return Decision(Action.NEW, candle)

        if candle.open_time < self.open_time:
            return Decision(Action.REJECT_STALE, candle)

        if candle.open_time == self.open_time:
            if event_time < self.event_time:
                return Decision(Action.REJECT_STALE, candle)   # an older frame arrived late
            if self.closed and not candle.closed:
                return Decision(Action.REJECT_STALE, candle)   # cannot reopen a closed candle
            self._set(candle, event_time)
            return Decision(Action.CLOSE if candle.closed else Action.UPDATE, candle)

        # candle.open_time > self.open_time: a newer candle has started.
        # If the previous one never closed we missed its final frame, so the gap
        # starts AT the previous open_time so that candle gets refetched too.
        gap_start = self.open_time + (self.interval_ms if self.closed else 0)
        gap_end = candle.open_time
        gap = (gap_start, gap_end) if gap_start < gap_end else None
        self._set(candle, event_time)
        return Decision(Action.NEW, candle, gap)

    def _set(self, candle: Candle, event_time: int):
        self.open_time = candle.open_time
        self.closed = candle.closed
        self.event_time = event_time
