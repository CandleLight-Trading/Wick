"""httpx wrapper that respects Binance's IP-scoped weight budget.

Binance reports how much of the 1200/min budget you have used in the
`x-mbx-used-weight-1m` response header. We read it after every call and pause
before crossing the soft limit. 429 means "slow down" (we back off and retry);
418 means "you ignored 429s and are banned" (we stop making calls entirely).
"""
import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)


class BannedError(RuntimeError):
    """Raised for every call once Binance has returned 418. Nothing recovers from this
    except waiting out the ban and restarting the process."""


class RestClient:
    def __init__(self, base_url: str, soft_limit: int, min_interval_s: float = 0.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=20.0)
        self._lock = asyncio.Lock()   # one call at a time keeps weight accounting simple
        self.soft_limit = soft_limit
        self.min_interval_s = min_interval_s   # venues without a weight header (Kraken: ~1 req/s)
        self._last_call = 0.0
        self.used_weight = 0
        self.weight_seen_at = 0.0
        self.calls = 0
        self.banned = False
        self.ban_message = ""

    async def get(self, path: str, params: dict | None = None):
        if self.banned:
            raise BannedError(self.ban_message)
        async with self._lock:
            await self._throttle()
            for attempt in range(6):
                if self.min_interval_s:
                    await asyncio.sleep(max(0.0, self._last_call + self.min_interval_s - time.time()))
                self._last_call = time.time()
                try:
                    resp = await self._client.get(path, params=params)
                except httpx.TransportError as e:
                    # DNS failure, connect error, read timeout: the network is flaky (a laptop
                    # waking from sleep, typically). Retry here so callers never see a blip.
                    wait = 2 ** attempt
                    log.warning("transport error on %s: %r, retrying in %ds", path, e, wait)
                    await asyncio.sleep(wait)
                    continue
                self.calls += 1
                self._record_weight(resp.headers)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 5)) + 1
                    log.warning("429 from %s, backing off %.0fs (attempt %d)", path, wait, attempt)
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code == 418:
                    self.banned = True
                    retry = resp.headers.get("Retry-After")
                    self.ban_message = f"IP BANNED by Binance (HTTP 418), Retry-After={retry}s. All REST calls stopped."
                    log.critical(self.ban_message)
                    raise BannedError(self.ban_message)
                resp.raise_for_status()
                return resp.json()
            raise RuntimeError(f"gave up on {path} after repeated 429s or transport errors")

    async def _throttle(self):
        # The header's window is the wall-clock minute. If we have not heard from the
        # server in over a minute our number is stale, so treat the budget as reset.
        if time.time() - self.weight_seen_at > 60:
            self.used_weight = 0
        if self.used_weight >= self.soft_limit:
            wait = 60 - (time.time() % 60) + 0.5
            log.info("used weight %d >= %d, sleeping %.1fs for the minute to roll",
                     self.used_weight, self.soft_limit, wait)
            await asyncio.sleep(wait)
            self.used_weight = 0

    def _record_weight(self, headers):
        w = headers.get("x-mbx-used-weight-1m")
        if w is not None:
            self.used_weight = int(w)
            self.weight_seen_at = time.time()

    def status(self) -> dict:
        return {"usedWeight1m": self.used_weight, "calls": self.calls,
                "banned": self.banned, "banMessage": self.ban_message}

    async def close(self):
        await self._client.aclose()
