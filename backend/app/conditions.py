"""Live, out-of-sample condition log.

Every time a 1h candle closes for a tracked symbol we evaluate the same conditions
the base-rate strip uses. If one just turned true (an onset), we record it with the
close price. Later closes fill in the +1h/+4h/+24h returns. After a month this table
is the only thing that can tell you whether the in-sample base rates meant anything:
it was written before the outcome was known.

It logs conditions, not recommendations. Nothing here says buy or sell.
"""
import logging

from .config import CONDITION_INTERVAL, HORIZONS_BARS
from .indicators import evaluate_conditions
from .store import Store
from .timeutil import interval_to_ms

log = logging.getLogger(__name__)

# Enough closed bars for the 720-bar volume average and 200MA to be defined, plus margin.
LOOKBACK_BARS = 24 * 30 + 250
RET_COLUMNS = {"1h": "ret_1h", "4h": "ret_4h", "24h": "ret_24h"}


class ConditionLog:
    def __init__(self, store: Store):
        self.store = store

    async def on_close(self, symbol: str):
        candles = await self.store.closed_history(symbol, CONDITION_INTERVAL, limit=LOOKBACK_BARS)
        if len(candles) < 2:
            return
        flags = evaluate_conditions(candles)
        last = candles[-1]
        for name, series in flags.items():
            if series[-1] and series[-2] is False:
                await self.store.insert_condition_event(symbol, name, last.open_time, last.close)
                log.info("condition onset: %s %s at %.6g", symbol, name, last.close)
        await self._resolve(symbol, candles)

    async def _resolve(self, symbol: str, candles):
        step = interval_to_ms(CONDITION_INTERVAL)
        close_at = {c.open_time: c.close for c in candles}
        for cond, open_time, price, r1, r4, r24 in await self.store.unresolved_condition_events(symbol):
            have = {"1h": r1, "4h": r4, "24h": r24}
            for label, bars in HORIZONS_BARS.items():
                target = open_time + bars * step
                if have[label] is None and target in close_at:
                    await self.store.set_condition_return(
                        symbol, cond, open_time, RET_COLUMNS[label], close_at[target] / price - 1.0)
