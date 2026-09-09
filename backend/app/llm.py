"""The researcher: one OpenAI Responses API call per symbol with bounded web search.

The quant side (playbooks) decides direction. The model only answers one question: does fresh
external information support, leave unchanged, or work against that setup? Output is a tiny
structured object (context, modifier, one reason, dated catalysts, two sentences), because
output tokens cost six times input and the UI composes the rest itself. "Nothing found" is
NEUTRAL, not a veto: that single rule is what stops research from becoming a compliance desk.

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
        "context": {"type": "string", "enum": ["supportive", "neutral", "adverse"],
                    "description": "does fresh external information support, leave unchanged, or work against the quantitative setup?"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "action_modifier": {"type": "string", "enum": ["strengthen", "unchanged", "weaken", "veto"],
                            "description": "veto only for something materially adverse: security incident, delisting, large unlock, regulatory action, credible fraud"},
        "key_reason": {"type": "string", "description": "one short plain-English sentence"},
        "catalysts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "properties": {"event": {"type": "string"},
                                                                "impact": {"type": "string", "enum": ["positive", "negative", "mixed"]},
                                                                "published_at": {"type": "string", "description": "ISO date/time if known, else 'unknown'"},
                                                                "age_hours": {"type": "number", "description": "hours before now; -1 if unknown"},
                                                                "url": {"type": "string"}},
                                                 "required": ["event", "impact", "published_at", "age_hours", "url"]}},
        "summary": {"type": "string", "description": "at most two plain sentences"},
    },
    "required": ["context", "confidence", "action_modifier", "key_reason", "catalysts", "summary"],
}

SYSTEM = """You are the external-context researcher for a quantitative trading terminal. A separate deterministic
system has already evaluated price, volume, flow, positioning and chart shape and produced the setup in the pack,
including which playbook it matched and its stance. Your task is NOT to redo technical analysis and NOT to
require a news catalyst.

Search for fresh information that materially changes the setup: project news, exchange or listing events,
token supply and unlock events, security incidents, regulatory events, ecosystem announcements, broad market
events, or credible explanations for the unusual price activity.

Classify the external context as SUPPORTIVE, NEUTRAL or ADVERSE for the stated stance.
NEUTRAL means no material fresh information was found. It must not by itself invalidate a quantitatively
strong setup. Use "veto" only for something materially adverse.

Freshness: the pack states the current UTC time. Prefer evidence published within the past 24 hours.
Information 24-72 hours old may be relevant. Anything older than 7 days is background only and must not be
presented as a fresh catalyst. Give published_at and age_hours for every catalyst when the source shows a date.

Search discipline: at most three searches. Stop as soon as you can classify the context. Do not write an essay."""

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
    return ("Setup pack (numbers computed locally from exchange data; prices in USDT):\n"
            + json.dumps(pack, separators=(",", ":"), default=str)
            + "\n\nClassify the external context for this setup and fill the schema.")


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
            "max_tool_calls": 3,                                                 # each search is metered separately
            "input": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_input(pack)}],
            "text": {"format": {"type": "json_schema", "name": "context_research", "schema": SCHEMA, "strict": True}},
            "max_output_tokens": 3000,   # search reasoning counts against this; the JSON itself is small
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
