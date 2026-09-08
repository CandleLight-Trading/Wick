"""The model judge: one OpenAI Responses API call per symbol with web search.

The model gets a compact evidence pack (the same numbers the rules judge used, plus the
base rates) and must fill a fixed JSON schema: what happened, why (with links), trend
type, stance, horizon, invalidation. Structured output keeps it from writing an essay,
and citations keep it honest about where the news came from. It still sounds confident
regardless of evidence; the recommendation log is the real check.

No SDK: the API is two HTTP calls and the raw shape is worth seeing.
"""
import json
import logging
from typing import Any

import httpx

from . import config
from .env import load_env

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stance": {"type": "string", "enum": ["long", "short", "flat"]},
        "action": {"type": "string", "enum": ["enter", "wait", "pass"],
                   "description": "enter = the current price is an acceptable entry; wait = thesis holds but entry is poor (extended, or needs a breakout); pass = no trade"},
        "thesis_verdict": {"type": "string", "enum": ["strengthened", "unchanged", "weakened"],
                           "description": "did the research strengthen or weaken the quantitative case?"},
        "main_risk": {"type": "string", "description": "one short sentence"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "horizon_hours": {"type": "integer"},
        "invalidation": {"type": "number", "description": "price at which the view is wrong"},
        "trend_type": {"type": "string", "enum": ["trending_up", "trending_down", "ranging", "breakout", "breakdown", "mean_reverting", "unclear"]},
        "summary": {"type": "string", "description": "two plain sentences: what is happening and why it matters"},
        "drivers": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "properties": {"text": {"type": "string"}, "url": {"type": "string"}},
                                               "required": ["text", "url"]}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "numbers_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stance", "action", "thesis_verdict", "main_risk", "confidence", "horizon_hours", "invalidation", "trend_type", "summary", "drivers", "risks", "numbers_used"],
}

SYSTEM = """You are a market analyst assisting a discretionary trader who trades crypto prop-firm challenges
(daily loss limit ~4%, max drawdown ~8%, holding 12-72 hours, perps allowed, funding is paid every 8h).
You will receive an evidence pack of numbers computed locally from exchange data, plus a rules-based verdict.
Use web search to find what happened to this asset in the last 24-48 hours: news, listings, unlocks, hacks,
macro, ETF flows, large liquidations. Prefer primary or reputable sources; include the URL for each driver.
The pack includes a geometric "shape" of the last three days (regression slope and R^2, efficiency ratio, variance
ratio, bandwidth squeeze, retrace after a drop, volume climax) and how that same shape resolved on this coin before.
Use it: a blow-off is exhaustion, not a trend to join; a squeeze implies a big move but not its direction.
Then decide a stance for the next 12-72 hours, and separately an ACTION: "enter" only if the current price is a
reasonable place to start the position; "wait" if the thesis holds but the entry is poor (already extended by more
than about one daily ATR, or a breakout has not confirmed); "pass" if there is no trade. Be specific and terse.
If evidence is thin, say flat / pass with low confidence.
Set invalidation as a price level, typically 1-2 daily ATRs from the current price, on the side that proves the view wrong.
Never recommend adding to a losing position. Mention funding cost if it works against the stance."""


POSITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["hold", "watch_closely", "reduce", "consider_exit"]},
        "reason": {"type": "string", "description": "one plain-English sentence a beginner understands"},
        "what_changed": {"type": "string", "description": "what is materially different since entry, one or two sentences"},
        "summary": {"type": "string", "description": "two or three plain sentences of context"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "risks": {"type": "array", "items": {"type": "string"}},
        "drivers": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "properties": {"text": {"type": "string"}, "url": {"type": "string"}},
                                               "required": ["text", "url"]}},
    },
    "required": ["action", "reason", "what_changed", "summary", "confidence", "risks", "drivers"],
}

SYSTEM_POSITION = """You are advising a discretionary trader who already HOLDS a position (details in the pack: side, entry,
stop, target, unrealised P&L in R, the original thesis, conditions at entry and now). Use web search for anything
material in the last 24-48 hours. Decide one of: hold, watch_closely, reduce, consider_exit. Lead with the action and
ONE plain-English reason a beginner understands; avoid jargon or explain it in the same sentence. Never suggest adding
to a losing position. Be concrete about what changed since entry. The trader decides; you advise."""


def load_api_key() -> str | None:
    return load_env().get("OPENAI_API_KEY")


def build_input(pack: dict) -> str:
    return ("Evidence pack (all numbers computed locally, UTC, prices in USDT):\n"
            + json.dumps(pack, indent=1, default=str)
            + "\n\nResearch the last 24-48h of news for this asset, then fill the schema.")


def parse_response(body: dict) -> dict:
    """Pull the JSON answer and any URL citations out of a Responses API body.
    Shape: {"output": [{"type": "web_search_call", ...}, {"type": "message", "content": [{"type": "output_text",
    "text": "...json...", "annotations": [{"type": "url_citation", "url": ..., "title": ...}]}]}], "usage": {...}}"""
    text, citations = None, []
    for item in body.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text = part.get("text")
                citations += [{"url": a.get("url"), "title": a.get("title")} for a in part.get("annotations", [])
                              if a.get("type") == "url_citation"]
            elif part.get("type") == "refusal":
                raise RuntimeError(f"model refused: {part.get('refusal')}")
    if text is None:
        raise RuntimeError(f"no message in response: {json.dumps(body)[:300]}")
    answer = json.loads(text)
    answer["citations"] = citations
    answer["usage"] = body.get("usage", {})
    answer["model"] = body.get("model")
    return answer


class OpenAIJudge:
    def __init__(self, api_key: str | None, model: str = config.OPENAI_MODEL, base: str = config.OPENAI_BASE):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(base_url=base, timeout=90.0,
                                         headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        self.status: dict[str, Any] = {"configured": bool(api_key), "model": model, "error": None, "verified": False}

    async def verify(self):
        """Check the configured model id exists. Surfaces a clear error in the UI instead of a
        mysterious 404 on the first real call."""
        if not self.api_key:
            self.status["error"] = "OPENAI_API_KEY not set (environment or backend/.env)"
            return
        try:
            r = await self._client.get("/v1/models")
            r.raise_for_status()
            ids = {m["id"] for m in r.json().get("data", [])}
            if self.model in ids:
                self.status["verified"] = True
            else:
                near = sorted(i for i in ids if i.startswith("gpt-5"))[:12]
                self.status["error"] = f"model {self.model!r} not found; some available: {near}"
        except Exception as e:
            self.status["error"] = f"could not verify model: {e!r}"

    async def analyze(self, pack: dict) -> dict:
        if not self.api_key:
            raise RuntimeError("no API key")
        req = {
            "model": self.model,
            "tools": [{"type": "web_search", "search_context_size": "low"}],   # "low" keeps cost down
            "input": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_input(pack)}],
            "text": {"format": {"type": "json_schema", "name": "market_analysis", "schema": SCHEMA, "strict": True}},
            "max_output_tokens": 6000,   # web-search reasoning counts against this; too low truncates the JSON
        }
        r = await self._client.post("/v1/responses", json=req)
        if r.status_code >= 400:
            raise RuntimeError(f"openai {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("status") == "incomplete":
            raise RuntimeError(f"response incomplete: {body.get('incomplete_details')}")
        answer = parse_response(body)
        self.status["error"] = None
        return answer

    async def analyze_position(self, pack: dict) -> dict:
        if not self.api_key:
            raise RuntimeError("no API key")
        req = {
            "model": self.model,
            "tools": [{"type": "web_search", "search_context_size": "low"}],
            "input": [{"role": "system", "content": SYSTEM_POSITION},
                      {"role": "user", "content": "Position pack:\n" + json.dumps(pack, indent=1, default=str) + "\n\nWhat should the holder do now, and why?"}],
            "text": {"format": {"type": "json_schema", "name": "position_review", "schema": POSITION_SCHEMA, "strict": True}},
            "max_output_tokens": 6000,
        }
        r = await self._client.post("/v1/responses", json=req)
        if r.status_code >= 400:
            raise RuntimeError(f"openai {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("status") == "incomplete":
            raise RuntimeError(f"response incomplete: {body.get('incomplete_details')}")
        answer = parse_response(body)
        self.status["error"] = None
        return answer

    async def close(self):
        await self._client.aclose()
