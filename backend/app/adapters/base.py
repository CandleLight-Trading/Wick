"""The ExchangeAdapter contract.

Everything exchange-specific lives behind this interface: URL shapes, stream naming,
symbol casing, JSON field names. IngestService, Store, Broadcaster and the API only
ever see the dataclasses in models.py. To add Kraken or a ccxt-backed adapter, implement
this class and change one line in main.py.
"""
from abc import ABC, abstractmethod

from ..models import Candle, Depth, KlineFrame, SymbolInfo, Ticker


class ExchangeAdapter(ABC):
    name: str
    # Binance encodes the stream list in the connect URL. Kraken connects to a fixed URL
    # and expects subscribe messages afterwards. IngestService checks this flag.
    subscribe_on_connect: bool = False

    # ---- REST -------------------------------------------------------------
    @abstractmethod
    async def fetch_symbols(self) -> list[SymbolInfo]: ...

    @abstractmethod
    async def fetch_klines(self, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int) -> list[Candle]:
        """Closed and in-progress candles with open_time in [start_ms, end_ms], oldest first."""

    @abstractmethod
    async def fetch_tickers(self) -> list[Ticker]: ...

    @abstractmethod
    async def fetch_depth(self, symbol: str, limit: int) -> Depth: ...

    # ---- WebSocket ---------------------------------------------------------
    @abstractmethod
    def ws_url(self, streams: list[str]) -> str: ...

    @abstractmethod
    def kline_stream(self, symbol: str, interval: str) -> str: ...

    @abstractmethod
    def ticker_stream(self, symbol: str) -> str: ...

    @abstractmethod
    def depth_stream(self, symbol: str) -> str: ...

    @abstractmethod
    def subscribe_messages(self, streams: list[str], req_id: int) -> list[str]:
        """Raw text frames to send to (un)subscribe. A list because some venues need one
        message per channel."""

    @abstractmethod
    def unsubscribe_messages(self, streams: list[str], req_id: int) -> list[str]: ...

    @abstractmethod
    def parse_message(self, raw: str) -> KlineFrame | Ticker | Depth | None:
        """Turn one raw WS text frame into a typed event, or None for control/ack messages."""
