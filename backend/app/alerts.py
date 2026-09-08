"""Alerts: an in-app feed always, plus Discord and/or Telegram when a webhook is configured.

Put one or both of these in backend/.env:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  TELEGRAM_BOT_TOKEN=123456:ABC...   TELEGRAM_CHAT_ID=123456789

Events: a coin enters the top movers, a judge opens a call, a paper position closes.
Each event has a key so restarts and rescans do not repeat it.
"""
import logging
from collections import deque

import httpx

from .timeutil import now_ms

log = logging.getLogger(__name__)


class Alerts:
    def __init__(self, env: dict[str, str]):
        self.discord = env.get("DISCORD_WEBHOOK_URL")
        self.tg_token = env.get("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env.get("TELEGRAM_CHAT_ID")
        self.configured = bool(self.discord or (self.tg_token and self.tg_chat))
        self._client = httpx.AsyncClient(timeout=15.0)
        self._sent: set[str] = set()
        self.recent: deque[dict] = deque(maxlen=100)
        self.errors = 0

    async def send(self, key: str, text: str, kind: str = "info"):
        if key in self._sent:
            return
        self._sent.add(key)
        self.recent.appendleft({"time": now_ms() // 1000, "kind": kind, "text": text})
        log.info("ALERT %s", text)
        try:
            if self.discord:
                await self._client.post(self.discord, json={"content": text[:1900]})
            if self.tg_token and self.tg_chat:
                await self._client.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                        json={"chat_id": self.tg_chat, "text": text[:4000]})
        except Exception as e:
            self.errors += 1
            log.warning("alert delivery failed: %r", e)

    def status(self) -> dict:
        return {"configured": self.configured, "discord": bool(self.discord),
                "telegram": bool(self.tg_token and self.tg_chat), "errors": self.errors}

    async def close(self):
        await self._client.aclose()
