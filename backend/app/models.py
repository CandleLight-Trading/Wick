"""Exchange-agnostic data types. Adapters produce these; nothing downstream sees raw JSON."""
from dataclasses import dataclass

from .timeutil import ms_to_s


@dataclass(slots=True)
class Candle:
    symbol: str        # canonical uppercase, e.g. BTCUSDT
    interval: str      # e.g. "1m"
    open_time: int     # ms since epoch
    open: float
    high: float
    low: float
    close: float
    volume: float      # base-asset volume
    closed: bool       # False while the candle is still forming
    taker_buy: float | None = None   # base volume bought by aggressors (Binance kline column 9 / k.V)

    def to_wire(self) -> dict:
        """Shape sent to the browser. Time is in seconds because that is what the chart wants."""
        return {
            "time": ms_to_s(self.open_time),
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "volume": self.volume, "closed": self.closed, "takerBuy": self.taker_buy,
        }


@dataclass(slots=True)
class KlineFrame:
    """One kline WebSocket message: the candle plus the exchange's event timestamp,
    which the state machine uses to reject out-of-order frames."""
    candle: Candle
    event_time: int


@dataclass(slots=True)
class Ticker:
    symbol: str
    last: float
    change_pct: float      # rolling 24h, NOT the UTC-day 1d candle
    quote_volume: float    # 24h volume in quote currency (USDT)
    high: float
    low: float
    event_time: int
    exchange: str = "binance"

    def to_wire(self) -> dict:
        return {
            "symbol": self.symbol, "last": self.last, "changePct": self.change_pct,
            "quoteVolume": self.quote_volume, "high": self.high, "low": self.low,
            "time": ms_to_s(self.event_time), "exchange": self.exchange,
        }


@dataclass(slots=True)
class Funding:
    """Perpetual-futures positioning snapshot. Funding is paid every 8h (Binance) or
    accrued continuously (Kraken); `funding_rate` is always expressed per 8h period."""
    symbol: str            # spot-style canonical symbol the perp tracks, e.g. BTCUSDT
    source: str            # which exchange's perp
    mark_price: float | None
    index_price: float | None
    funding_rate: float | None       # fraction per 8h, e.g. 0.0001 = 0.01%
    next_funding_time: int | None    # ms, None if the venue accrues continuously
    open_interest: float | None      # in base units (contracts of 1 base unit)
    time: int

    def to_wire(self) -> dict:
        return {
            "symbol": self.symbol, "source": self.source, "markPrice": self.mark_price,
            "indexPrice": self.index_price, "fundingRate": self.funding_rate,
            "nextFundingTime": ms_to_s(self.next_funding_time) if self.next_funding_time else None,
            "openInterest": self.open_interest, "time": ms_to_s(self.time),
        }


@dataclass(slots=True)
class FundingPoint:
    time: int      # ms
    rate: float    # fraction per 8h period


@dataclass(slots=True)
class Depth:
    symbol: str
    bids: list[tuple[float, float]]   # (price, qty), best first
    asks: list[tuple[float, float]]
    ts: int

    def to_wire(self) -> dict:
        return {"symbol": self.symbol, "bids": self.bids[:20], "asks": self.asks[:20], "time": ms_to_s(self.ts)}


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    status: str
    tick_size: float
    step_size: float
