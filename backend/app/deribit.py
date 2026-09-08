"""Implied volatility from Deribit's public DVOL index (BTC and ETH only).

DVOL is a 30-day implied volatility index, annualised, in percent (38.7 = 38.7%).
Compared with our realised 30-day vol it gives the variance risk premium: implied above
realised means options are pricing more movement than has been happening, which is the
normal state; implied below realised is unusual and worth noticing.
"""
import asyncio
import logging

import httpx

from .store import Store

log = logging.getLogger(__name__)

INDEXES = {"BTC": "btcdvol_usdc", "ETH": "ethdvol_usdc"}


async def dvol_poll(store: Store, base_url: str, interval_s: int):
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        while True:
            for asset, name in INDEXES.items():
                try:
                    r = await client.get("/api/v2/public/get_index_price", params={"index_name": name})
                    r.raise_for_status()
                    store.dvol[asset] = float(r.json()["result"]["index_price"])
                except Exception as e:
                    log.warning("deribit DVOL %s failed: %r", asset, e)
            await asyncio.sleep(interval_s)
