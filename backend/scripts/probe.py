"""Provider probe. Run this FROM THE HOST REGION before choosing where Wick lives.

    uv run python scripts/probe.py

It hits every external endpoint Wick depends on and prints a verdict per provider. A
451 or 403 means that provider does not serve this location; Wick must then run with a
different primary source (Kraken) rather than be placed somewhere merely to get around it.
"""
import asyncio
import json
import sys

import httpx
import websockets

CHECKS = [
    ("binance spot REST", "GET", "https://data-api.binance.vision/api/v3/ping"),
    ("binance spot klines", "GET", "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1"),
    ("binance spot WS", "WS", "wss://data-stream.binance.vision/ws/btcusdt@ticker"),
    ("binance futures (optional)", "GET", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    ("kraken spot REST", "GET", "https://api.kraken.com/0/public/Time"),
    ("kraken spot WS", "WS", "wss://ws.kraken.com/v2"),
    ("kraken futures", "GET", "https://futures.kraken.com/derivatives/api/v3/tickers"),
    ("deribit DVOL", "GET", "https://www.deribit.com/api/v2/public/get_index_price?index_name=btcdvol_usdc"),
    ("openai", "GET", "https://api.openai.com/v1/models"),
]
REQUIRED = {"binance spot REST", "binance spot klines", "binance spot WS", "kraken spot REST", "kraken spot WS", "kraken futures"}


async def probe() -> dict:
    results = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for name, kind, url in CHECKS:
            try:
                if kind == "WS":
                    async with websockets.connect(url, open_timeout=15) as ws:
                        await asyncio.wait_for(ws.recv(), 15)
                    results[name] = {"ok": True, "detail": "connected, first frame received"}
                else:
                    r = await client.get(url)
                    # OpenAI without a key answers 401: reachable is what we are testing here.
                    ok = r.status_code < 400 or (name == "openai" and r.status_code == 401)
                    results[name] = {"ok": ok, "detail": f"HTTP {r.status_code}" + ("" if ok else f": {r.text[:120]}")}
            except Exception as e:
                results[name] = {"ok": False, "detail": repr(e)[:160]}
    return results


def verdict(results: dict) -> tuple[bool, str]:
    missing = [n for n in REQUIRED if not results.get(n, {}).get("ok")]
    if not missing:
        return True, "All required providers reachable. This region can host Wick as configured."
    if any(n.startswith("binance spot") for n in missing) and all(not n.startswith("kraken") for n in missing):
        return False, ("Binance spot is not served here. Do not relocate to route around it; run Wick with Kraken as the "
                       "primary spot source in this region, or pick a region/provider where the official market-data host answers.")
    return False, f"Required providers unreachable: {', '.join(missing)}. Choose a different region or provider."


async def main():
    results = await probe()
    for name, r in results.items():
        print(f"{'OK  ' if r['ok'] else 'FAIL'} {name:28} {r['detail']}")
    ok, msg = verdict(results)
    print("\n" + msg)
    print(json.dumps({"ok": ok, "results": results}))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
