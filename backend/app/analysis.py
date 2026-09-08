"""Orchestrates the Analysis tab: scan, judge, log, resolve, alert. Runs every SCAN_INTERVAL_S.

  scan()     features + shape + unusualness for every tracked symbol -> movers ranking
             breadth across the tracked set; depth + slippage for the top movers
             rules verdict for every symbol -> recommendation log (on stance change)
             top movers without a fresh analysis -> model judge (budget permitting)
             resolve open paper positions against closed 1h candles
             alerts for new top movers, new calls, closed positions

Nothing here blocks a request: the API reads the last results from memory and SQLite.
"""
import asyncio
import logging

from . import config
from .alerts import Alerts
from .flow import breadth, daily_tracker, slippage_table
from .indicators import base_rates, evaluate_conditions
from .llm import OpenAIJudge
from .paper import find_exit, forward_returns, pnl_pct
from .scanner import Features, compute_features, rules_verdict, unusualness
from .shape import shape_report
from .store import Store
from .timeutil import now_ms

log = logging.getLogger(__name__)
BTC = "BTCUSDT"


def estimate_cost(usage: dict) -> float:
    """Rough dollars per call from token counts plus one web search."""
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return inp / 1e6 * config.LLM_INPUT_USD_PER_M + out / 1e6 * config.LLM_OUTPUT_USD_PER_M + config.LLM_SEARCH_USD_PER_CALL


class AnalysisService:
    def __init__(self, store: Store, judge: OpenAIJudge, tracked_fn, secondary_name: str | None,
                 adapter=None, alerts: Alerts | None = None):
        self.store = store
        self.judge = judge
        self.tracked_fn = tracked_fn
        self.secondary = secondary_name
        self.adapter = adapter            # for depth snapshots of the top movers
        self.alerts = alerts
        self.desk = None                  # set by main; setups refresh after each scan
        self.movers: list[dict] = []          # last scan, sorted by unusualness
        self.rules: dict[str, dict] = {}      # symbol -> latest rules verdict
        self.features: dict[str, Features] = {}
        self.shapes: dict[str, dict] = {}     # symbol -> shape_report, refreshed each scan
        self.slippage: dict[str, dict] = {}   # symbol -> slippage table (top movers only)
        self.breadth: dict = {}
        self.last_scan_ms = 0
        self._top: set[str] = set()
        self._lock = asyncio.Lock()
        self._in_flight: set[str] = set()     # symbols with a model call in progress

    # ---- features ---------------------------------------------------------------
    def _features(self, symbol: str) -> Features | None:
        h1 = [c for c in self.store.ring(symbol, "1h") if c.closed]
        d1 = [c for c in self.store.ring(symbol, "1d") if c.closed]
        other = self.store.other_tickers.get(self.secondary, {}).get(symbol) if self.secondary else None
        btc_d1 = [c for c in self.store.ring(BTC, "1d") if c.closed] if symbol != BTC else None
        return compute_features(symbol, h1, d1, self.store.tickers.get(symbol), self.store.funding.get(symbol), other,
                                btc_d1=btc_d1, btc_ticker=self.store.tickers.get(BTC))

    async def evidence_pack(self, symbol: str, f: Features) -> dict:
        h1 = await self.store.closed_history(symbol, "1h")
        conds = evaluate_conditions(h1)
        rates = base_rates(h1, conds)
        hist = self.store.funding_history.get(symbol, [])
        slip = self.slippage.get(symbol)
        return {
            "symbol": symbol, "asOfUtc": now_ms() // 1000, "features": f.to_wire(),
            "marketBreadth": self.breadth,
            "rulesVerdict": {k: v for k, v in self.rules.get(symbol, {}).items() if k != "checks"},
            "rulesChecks": [f"{c['name']}: {'pass' if c['ok'] else 'fail'} ({c['detail']})" for c in self.rules.get(symbol, {}).get("checks", [])],
            "conditionsTrueNow": [r["condition"] for r in rates],
            "baseRates24hNet": {r["condition"]: {"hitRate": r["horizons"]["24h"].get("hitRateNet"), "n": r["horizons"]["24h"]["n"],
                                                 "secondHalfHitRate": r["horizons"]["24h"].get("secondHalf", {}).get("hitRateNet")} for r in rates},
            "funding7dPositiveShare": (sum(1 for p in hist if p.rate > 0) / len(hist)) if hist else None,
            "impliedVol30dPct": self.store.dvol.get(symbol.replace("USDT", "")),
            "slippageBpsAtAccountSize": next((r for r in (slip or {}).get("rows", []) if r["notional"] == config.ACCOUNT_SIZE_USD), None),
            "recentTrackRecord": await self.store.rec_hit_rates(symbol),
            "shape": self._shape_for_pack(symbol),
        }

    def _shape_for_pack(self, symbol: str) -> dict | None:
        s = self.shapes.get(symbol)
        if not s:
            return None
        h24 = s["horizons"]["24h"]
        return {"name": s["name"], "description": s["description"],
                "metrics": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s["metrics"].items()},
                "onThisSymbolBefore": {"episodes": s["nEpisodes"], "hitRateNet24h": h24.get("hitRateNet"),
                                       "ci95": [h24.get("ciLo"), h24.get("ciHi")], "medianNet24h": h24.get("medianNet"),
                                       "secondHalfHitRate": h24.get("secondHalf", {}).get("hitRateNet"),
                                       "underpowered": h24["underpowered"]}}

    async def _refresh_shape(self, symbol: str):
        h1 = await self.store.closed_history(symbol, "1h")
        # Pure Python over ~17,500 bars takes a few hundred ms; keep it off the event loop.
        report = await asyncio.to_thread(shape_report, h1)
        if report:
            self.shapes[symbol] = report

    async def _refresh_depth(self, symbol: str):
        if self.adapter is None:
            return
        try:
            depth = await self.adapter.fetch_depth(symbol, config.DEPTH_LIMIT)
            self.store.depth_scan[symbol] = depth
            notionals = sorted(set(config.SLIPPAGE_NOTIONALS + [config.ACCOUNT_SIZE_USD]))
            table = slippage_table(depth, notionals)
            if table:
                self.slippage[symbol] = table
        except Exception as e:
            log.warning("depth for %s failed: %r", symbol, e)

    # ---- scan ----------------------------------------------------------------------
    async def run_loop(self, ready: asyncio.Event | None = None):
        # First scan only once stored history is continuous (ALIVE -> READY), otherwise the
        # scanner would rank coins on gappy data; a second one after the boot-time flow
        # baseline has landed, then the regular cadence.
        if ready is not None:
            await ready.wait()
        for delay in (5, 120):
            await asyncio.sleep(delay)
            try:
                await self.scan()
            except Exception:
                log.exception("analysis scan failed")
        while True:
            await asyncio.sleep(config.SCAN_INTERVAL_S)
            try:
                await self.scan()
            except Exception:
                log.exception("analysis scan failed")

    async def scan(self):
        async with self._lock:
            now = now_ms()
            ranked = []
            for sym in sorted(self.tracked_fn()):
                f = self._features(sym)
                if f is None:
                    continue
                self.features[sym] = f
                await self._refresh_shape(sym)
                verdict = rules_verdict(f, self.shapes.get(sym))
                self.rules[sym] = verdict
                await self._log_if_changed(sym, "rules", verdict["stance"], verdict["horizonH"], verdict["invalidation"], f, now)
                ranked.append({"symbol": sym, "score": unusualness(f), "features": f.to_wire()})
            ranked.sort(key=lambda r: r["score"], reverse=True)
            self.movers = ranked
            self.breadth = breadth([r["features"] for r in ranked])
            self.last_scan_ms = now
            await self._resolve_open()
            top = [r["symbol"] for r in ranked[: config.TOP_MOVERS]]
            for sym in top:
                await self._refresh_depth(sym)
            for sym in set(top) - self._top:
                if self._top and self.alerts:      # not on the very first scan
                    f = self.features[sym]
                    await self.alerts.send(f"mover:{sym}:{now // 3_600_000}",
                                           f"📈 {sym} entered the top movers: {f.change24hPct:+.1f}% 24h, {f.moveAtr:+.1f} ATR, vol {f.volMultiple:.1f}x", "mover")
            self._top = set(top)
            if config.AUTO_RESEARCH:
                for sym in top:
                    await self.maybe_research(sym, force=False, trigger="auto")
            if self.desk:
                await self.desk.refresh_setups()

    # ---- model judge ------------------------------------------------------------
    async def maybe_research(self, symbol: str, force: bool, trigger: str = "manual") -> dict | None:
        """Model call for one symbol, if fresh enough analysis is missing and budget allows.
        `trigger` is recorded on the row so the usage log says who spent the call."""
        if not self.judge.status.get("configured"):
            return None
        latest = await self.store.latest_analysis(symbol)
        if not force and latest and now_ms() - latest["time"] < config.LLM_RESEARCH_TTL_S * 1000:
            return latest
        # No daily cap: every call is a click. Calls are counted, not limited.
        f = self.features.get(symbol) or self._features(symbol)
        if f is None or symbol in self._in_flight:
            return latest
        self._in_flight.add(symbol)
        try:
            answer = await self.judge.analyze(await self.evidence_pack(symbol, f))
        except Exception as e:
            self.judge.status["error"] = str(e)[:300]
            log.warning("model judge failed for %s: %s", symbol, e)
            return latest
        finally:
            self._in_flight.discard(symbol)
        row = {
            "symbol": symbol, "time": now_ms(), "model": answer.get("model") or self.judge.model,
            "stance": answer["stance"], "confidence": answer["confidence"],
            "action": answer.get("action", "pass" if answer["stance"] == "flat" else "enter"),
            "thesisVerdict": answer.get("thesis_verdict", "unchanged"), "mainRisk": answer.get("main_risk", ""),
            "horizonH": max(12, min(72, int(answer["horizon_hours"]))), "invalidation": float(answer["invalidation"]),
            "trend": answer["trend_type"], "summary": answer["summary"], "price": f.price,
            "drivers": answer["drivers"], "risks": answer["risks"], "numbers": answer["numbers_used"],
            "citations": answer.get("citations", []), "usage": answer.get("usage", {}), "trigger": trigger,
            "shapeAtResearch": (self.shapes.get(symbol) or {}).get("label"),
            "rulesStanceAtResearch": (self.rules.get(symbol) or {}).get("stance"),
        }
        await self.store.insert_analysis(row)
        await self._log_if_changed(symbol, "model", row["stance"], row["horizonH"], row["invalidation"], f, row["time"])
        log.info("model judge %s: %s (%s), %s", symbol, row["stance"], row["confidence"], row["summary"][:80])
        return row

    # ---- recommendation log + paper positions ---------------------------------
    async def _log_if_changed(self, symbol, source, stance, horizon_h, invalidation, f: Features, now):
        open_rec = await self.store.open_recommendation(symbol, source)
        if open_rec and open_rec["stance"] == stance:
            return
        if open_rec:
            pnl = pnl_pct(open_rec["stance"], open_rec["entry"], f.price, (now - open_rec["time"]) / 3_600_000, open_rec["funding_rate"])
            reason = "flip" if stance != "flat" else "flat"
            await self.store.close_recommendation(open_rec["id"], now, f.price, reason, pnl)
            if self.alerts:
                await self.alerts.send(f"close:{open_rec['id']}", f"⏹ {symbol} {source} {open_rec['stance']} closed ({reason}) at {f.price:.6g}: {pnl * 100:+.2f}% net", "close")
        if stance != "flat":
            await self.store.insert_recommendation(symbol, source, now, stance, horizon_h, f.price, invalidation, f.fundingRate)
            if self.alerts:
                inv = f"{invalidation:.6g}" if invalidation else "n/a"
                await self.alerts.send(f"call:{symbol}:{source}:{now}", f"🎯 {symbol} {source} judge: {stance.upper()} at {f.price:.6g}, invalidation {inv}, {horizon_h}h", "call")

    async def _resolve_open(self):
        for rec in await self.store.open_recommendations():
            candles = [c for c in self.store.ring(rec["symbol"], "1h") if c.closed and c.open_time >= rec["time"] - 3_600_000]
            ex = find_exit(rec["stance"], rec["time"], rec["invalidation"], rec["horizon_h"], candles)
            rets = forward_returns(rec["stance"], rec["time"] - rec["time"] % 3_600_000, rec["entry"], candles)
            await self.store.update_recommendation_returns(rec["id"], rets)
            if ex:
                closed_at = ex.time + 3_600_000
                pnl = pnl_pct(rec["stance"], rec["entry"], ex.price, (closed_at - rec["time"]) / 3_600_000, rec["funding_rate"])
                await self.store.close_recommendation(rec["id"], closed_at, ex.price, ex.reason, pnl)
                if self.alerts:
                    await self.alerts.send(f"close:{rec['id']}", f"⏹ {rec['symbol']} {rec['source']} {rec['stance']} closed ({ex.reason}) at {ex.price:.6g}: {pnl * 100:+.2f}% net", "close")

    async def _tracker(self) -> dict:
        """Today's paper account against the prop rules, both judges combined."""
        recs = await self.store.recommendations(limit=5000)
        day_start = (now_ms() // 86_400_000) * 86_400_000
        closed = sorted((r for r in recs if r["closed_at"] is not None), key=lambda r: r["closed_at"])
        equity, peak = config.PAPER_START_EQUITY, config.PAPER_START_EQUITY
        for r in closed:
            equity += r["pnl_pct"] * config.PAPER_NOTIONAL
            peak = max(peak, equity)
        today = sum(r["pnl_pct"] * config.PAPER_NOTIONAL for r in closed if r["closed_at"] >= day_start)
        unreal = 0.0
        for r in recs:
            if r["closed_at"] is None:
                f = self.features.get(r["symbol"])
                if f:
                    unreal += pnl_pct(r["stance"], r["entry"], f.price, (now_ms() - r["time"]) / 3_600_000, r["funding_rate"]) * config.PAPER_NOTIONAL
        return daily_tracker(today, unreal, config.PAPER_START_EQUITY, config.PROP_DAILY_LOSS_LIMIT_PCT,
                             config.PROP_MAX_DRAWDOWN_PCT, peak, equity + unreal)

    # ---- research provenance ------------------------------------------------------
    def _setup_wire(self, s: dict | None) -> dict | None:
        """Setup plus an explicit research state: none / fresh / aging / stale, with reasons.
        Stale means time has passed OR the market moved materially since the research ran."""
        if not s:
            return None
        p = s["payload"]
        r = p.get("research")
        sym = s["symbol"]
        research_state, reasons, age_s = "none", [], None
        if r and r.get("time"):
            age_s = (now_ms() - r["time"]) / 1000
            f = self.features.get(sym)
            if f and r.get("price") and f.atrDailyPct:
                moved = abs(f.price / r["price"] - 1) * 100
                if moved > f.atrDailyPct:
                    reasons.append(f"price moved {moved:.1f}% since research (more than one daily ATR)")
            shape_now = (self.shapes.get(sym) or {}).get("label")
            if r.get("shapeAtResearch") and shape_now and shape_now != r["shapeAtResearch"]:
                reasons.append(f"shape changed from {r['shapeAtResearch'].replace('_', ' ')} to {shape_now.replace('_', ' ')}")
            stance_now = (self.rules.get(sym) or {}).get("stance")
            if r.get("rulesStanceAtResearch") and stance_now and stance_now != r["rulesStanceAtResearch"] and stance_now != "flat":
                reasons.append(f"quant signal flipped to {stance_now}")
            if reasons or age_s > config.RESEARCH_AGING_S:
                research_state = "stale"
                if age_s > config.RESEARCH_AGING_S:
                    reasons.append(f"{age_s / 3600:.0f}h old")
            elif age_s > config.RESEARCH_FRESH_S:
                research_state = "aging"
            else:
                research_state = "fresh"
        return {"id": s["id"], "state": s["state"], "quality": s["quality"], "bias": s["bias"], "concern": s["concern"],
                "recommendation": s["recommendation"], "detectedAt": s["detected_at"] // 1000, "timeframe": s["timeframe"],
                "horizonH": s["horizon_h"], "checks": p.get("checks", []), "research": r,
                "researchState": research_state, "researchAgeS": age_s, "staleReasons": reasons, "pinned": bool(p.get("pinned")),
                "researchTrigger": (r or {}).get("trigger"), "liveSignal": (self.rules.get(sym) or {}).get("stance", "flat")}

    async def llm_usage(self) -> dict:
        """Today's model calls with token counts and an estimated cost, newest first."""
        day_start = (now_ms() // 86_400_000) * 86_400_000
        rows = await self.store.rows("analyses", "time >= ?", (day_start,), order="time DESC")
        calls = []
        for a in rows:
            u = a["payload"].get("usage") or {}
            calls.append({"symbol": a["symbol"], "time": a["time"] // 1000, "trigger": a["payload"].get("trigger", "manual"),
                          "inputTokens": u.get("input_tokens") or u.get("prompt_tokens") or 0,
                          "outputTokens": u.get("output_tokens") or u.get("completion_tokens") or 0,
                          "estCostUsd": estimate_cost(u)})
        return {"calls": len(calls), "estCostUsd": sum(c["estCostUsd"] for c in calls), "recent": calls[:20],
                "autoResearch": config.AUTO_RESEARCH}

    # ---- payload for the tab -------------------------------------------------------
    async def payload(self) -> dict:
        analyses = {a["symbol"]: a for a in await self.store.latest_analyses()}
        setups = await self.desk.active_setups() if self.desk else {}
        held = {}
        if self.desk:
            for t in await self.store.rows("trades", "state IN ('waiting','ready','open')"):
                held.setdefault(t["symbol"], []).append({"tradeId": t["id"], "accountId": t["account_id"], "state": t["state"],
                                                          "side": t["side"], "status": t["status"],
                                                          "unrealizedUsd": self.desk._unrealized(t) if t["state"] == "open" else None,
                                                          "unrealizedR": (self.desk._unrealized(t) / t["risk_usd"]) if t["state"] == "open" and t["risk_usd"] else None})
        return {
            "desk": await self.desk.briefing() if self.desk else None,
            "lastScan": self.last_scan_ms // 1000, "scanIntervalS": config.SCAN_INTERVAL_S,
            "llm": {**self.judge.status, "callsToday": await self.store.analyses_today(), "dailyCap": None,
                    "topMovers": config.TOP_MOVERS, "ttlH": config.LLM_RESEARCH_TTL_S // 3600, "usage": await self.llm_usage()},
            "movers": [{**m, "rules": self.rules.get(m["symbol"]), "model": analyses.get(m["symbol"]),
                        "shape": self.shapes.get(m["symbol"]), "slippage": self.slippage.get(m["symbol"]),
                        "setup": self._setup_wire(setups.get(m["symbol"])), "held": held.get(m["symbol"], [])} for m in self.movers],
            "breadth": self.breadth, "tracker": await self._tracker(), "dvol": self.store.dvol,
            "alerts": {**(self.alerts.status() if self.alerts else {"configured": False}), "recent": list(self.alerts.recent)[:30] if self.alerts else []},
            "dailyLossBudgetPct": config.DAILY_LOSS_BUDGET_PCT, "accountSize": config.ACCOUNT_SIZE_USD,
        }
