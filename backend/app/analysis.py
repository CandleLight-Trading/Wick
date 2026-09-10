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
        self._shape_key: dict[str, int] = {}       # symbol -> last closed 1h open_time the shape was computed for
        self.slippage: dict[str, dict] = {}   # symbol -> slippage table (top movers only)
        self.breadth: dict = {}
        self.last_scan_ms = 0
        self._top: set[str] = set()
        self._lock = asyncio.Lock()
        self._in_flight: set[str] = set()     # symbols with a model call in progress
        self.broadcaster = None               # set by main; research and scan events are pushed to open pages
        self.jobs: dict[str, dict] = {}       # symbol -> research job (queued | running | done | failed)
        self._sem = asyncio.Semaphore(config.RESEARCH_CONCURRENCY)
        self.scan_stats: dict = {"checked": 0, "interesting": 0}
        self.warming: set[str] = set()        # symbols being tracked and backfilled right now

    # ---- features ---------------------------------------------------------------
    def _features(self, symbol: str) -> Features | None:
        h1 = [c for c in self.store.ring(symbol, "1h") if c.closed]
        d1 = [c for c in self.store.ring(symbol, "1d") if c.closed]
        other = self.store.other_tickers.get(self.secondary, {}).get(symbol) if self.secondary else None
        btc_d1 = [c for c in self.store.ring(BTC, "1d") if c.closed] if symbol != BTC else None
        return compute_features(symbol, h1, d1, self.store.tickers.get(symbol), self.store.funding.get(symbol), other,
                                btc_d1=btc_d1, btc_ticker=self.store.tickers.get(BTC))

    async def evidence_pack(self, symbol: str, f: Features) -> dict:
        from datetime import datetime, timezone
        rules = self.rules.get(symbol, {})
        r = lambda v, d=2: round(v, d) if isinstance(v, float) else v
        # Compact on purpose: input is cheap but not free, and the model is not asked to redo the numbers.
        return {
            "symbol": symbol, "nowUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": f.price, "change24hPct": r(f.change24hPct), "moveDailyAtr": r(f.moveAtr), "volumeVs30d": r(f.volMultiple),
            "rsi1h": r(f.rsi, 0), "fundingPer8h": f.fundingRate, "takerBuyShare24h": r(f.takerBuyRatio24),
            "ownMoveVsBtcPct": r(f.residual24hPct), "impliedVol30dPct": self.store.dvol.get(symbol.replace("USDT", "")),
            "quant": {"playbook": rules.get("playbook"), "riskCharacter": rules.get("riskCharacter"), "stance": rules.get("stance"),
                      "entry": rules.get("entryAction"), "summary": rules.get("summary")},
            "shape": {"name": s["name"], "hitRateNet24hOnThisCoin": s["horizons"]["24h"].get("hitRateNet")} if (s := self.shapes.get(symbol)) else None,
            "marketBreadth": self.breadth,
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
        # Reading 17,500 bars and labelling them costs about 0.6 s per coin, which made a
        # scan of 36 coins take 25 s. The shape only changes when a 1h candle closes, so
        # recompute only then; a rescan between closes (adding a coin, say) is near instant.
        key = self.store.last_closed_open_time(symbol, "1h")
        if key is not None and self._shape_key.get(symbol) == key:
            return
        h1 = await self.store.closed_history(symbol, "1h")
        report = await asyncio.to_thread(shape_report, h1)   # pure Python; keep it off the event loop
        if report:
            self.shapes[symbol] = report
            self._shape_key[symbol] = key

    async def _refresh_depth(self, symbol: str):
        if self.adapter is None:
            return
        cur = self.store.depth_scan.get(symbol)
        if cur and now_ms() - cur.ts < 5 * 60_000:      # weight 50 a call; five minutes is fresh enough for slippage
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

    async def scan(self, only: set[str] | None = None):
        """Full pass over every tracked coin, or (with `only`) recompute just those and re-rank
        the rest from their last features: the path a freshly added coin takes."""
        async with self._lock:
            now = now_ms()
            ranked = []
            for sym in sorted(self.tracked_fn()):
                if only is not None and sym not in only and sym in self.features:
                    f = self.features[sym]
                else:
                    f = self._features(sym)
                    if f is None:
                        continue
                    self.features[sym] = f
                    await self._refresh_shape(sym)
                    verdict = rules_verdict(f, self.shapes.get(sym))
                    self.rules[sym] = verdict
                    await self._log_if_changed(sym, "rules", verdict["stance"], verdict["horizonH"], verdict["invalidation"], f, now)
                ranked.append({"symbol": sym, "score": unusualness(f), "features": f.to_wire()})
            self.scan_stats = {"checked": len(ranked), "interesting": sum(1 for r in ranked if (self.rules.get(r["symbol"]) or {}).get("stance", "flat") != "flat")}
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
        if self.broadcaster:
            self.broadcaster.publish_all({"type": "scan", "lastScan": now // 1000})

    # ---- research jobs -----------------------------------------------------------
    def start_research(self, symbol: str, user_id: int | None = None, trigger: str = "manual") -> dict:
        """Click -> job. Returns at once; the call runs on the server whatever the browser does.
        Jobs are per user and symbol: a second click on a running one returns the same job."""
        key = f"{user_id}:{symbol}"
        job = self.jobs.get(key)
        if job and job["state"] in ("queued", "running"):
            return job
        job = self.jobs[key] = {"symbol": symbol, "userId": user_id, "state": "queued", "startedAt": now_ms(), "finishedAt": None,
                                "error": None, "trigger": trigger}
        asyncio.create_task(self._run_job(job))
        return job

    async def _run_job(self, job: dict):
        sym = job["symbol"]
        self._push_job(job)
        async with self._sem:
            job["state"] = "running"
            self._push_job(job)
            try:
                row = await self.maybe_research(sym, force=True, trigger=job["trigger"], raise_errors=True, user_id=job["userId"])
                if row is None:
                    raise RuntimeError(self.judge.status.get("error") or "model judge unavailable")
                job["state"] = "done"
            except Exception as e:
                job["state"], job["error"] = "failed", str(e)[:300]
                log.warning("research job %s failed: %s", sym, e)
            job["finishedAt"] = now_ms()
        self._push_job(job)

    def _push_job(self, job: dict):
        if self.broadcaster:      # private: only this user's sockets hear about their research
            self.broadcaster.publish_user(job["userId"], {"type": "research", **{k: v for k, v in job.items() if k != "userId"}})

    def jobs_for(self, user_id: int | None) -> dict[str, dict]:
        return {j["symbol"]: j for j in self.jobs.values() if j["userId"] == user_id}

    def jobs_summary(self, user_id: int | None = None) -> dict:
        states = [j["state"] for j in self.jobs.values() if j["userId"] == user_id]
        return {"running": states.count("running"), "queued": states.count("queued")}

    # ---- model judge ------------------------------------------------------------
    async def maybe_research(self, symbol: str, force: bool, trigger: str = "manual", raise_errors: bool = False,
                             user_id: int | None = None) -> dict | None:
        """Model call for one symbol, if fresh enough analysis is missing and budget allows.
        `trigger` is recorded on the row so the usage log says who spent the call."""
        if not self.judge.status.get("configured"):
            return None
        latest = await self.store.latest_analysis(symbol, user_id)
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
            if raise_errors:
                raise
            return latest
        finally:
            self._in_flight.discard(symbol)
        row = self.compose_row(symbol, f, answer, trigger)
        row["userId"] = user_id
        await self.store.insert_analysis(row)
        await self._log_if_changed(symbol, "model", row["stance"], row["horizonH"], row["invalidation"], f, row["time"])
        log.info("model judge %s: %s (%s), %s", symbol, row["stance"], row["confidence"], row["summary"][:80])
        return row

    def compose_row(self, symbol: str, f: Features, answer: dict, trigger: str) -> dict:
        """The quant side owns direction and entry; research only modifies it.
          quant flat            -> WAIT if context is supportive (worth watching), else PASS
          modifier veto         -> PASS, adverse reason shown
          modifier weaken       -> WAIT (the idea holds, not at this price / not yet)
          strengthen/unchanged  -> the playbook's own entry action (enter, or wait if extended)"""
        rules = self.rules.get(symbol) or {}
        q_stance, q_action = rules.get("stance", "flat"), rules.get("entryAction", "enter")
        ctx, mod = answer.get("context", "neutral"), answer.get("action_modifier", "unchanged")
        if q_stance == "flat":
            stance, action = "flat", ("wait" if ctx == "supportive" else "pass")
        elif mod == "veto":
            stance, action = q_stance, "pass"
        elif mod == "weaken":
            stance, action = q_stance, "wait"
        else:
            stance, action = q_stance, q_action
        cats = answer.get("catalysts", [])
        negative = [c for c in cats if c.get("impact") == "negative"]

        def age(c):
            h = c.get("age_hours", -1)
            return "" if h is None or h < 0 else f" ({h:.0f}h ago)" if h < 48 else f" ({h / 24:.0f}d ago)"
        drivers = [{"text": f"{c['event']}{age(c)}", "url": c.get("url", "")} for c in cats]
        inv = rules.get("invalidation") or (f.price * (1 - 1.5 * (f.atrDailyPct or 3) / 100) if stance != "short" else f.price * (1 + 1.5 * (f.atrDailyPct or 3) / 100))
        trend_map = {"trend_up": "trending_up", "flag_up": "trending_up", "trend_down": "trending_down", "flag_down": "trending_down",
                     "blowoff_up": "breakout", "capitulation": "breakdown", "range": "ranging", "chop": "ranging", "squeeze": "ranging"}
        return {
            "symbol": symbol, "time": now_ms(), "model": answer.get("model") or self.judge.model,
            "stance": stance, "confidence": answer.get("confidence", "low"), "action": action,
            "thesisVerdict": {"strengthen": "strengthened", "weaken": "weakened", "veto": "weakened"}.get(mod, "unchanged"),
            "mainRisk": answer.get("key_reason", "") if ctx == "adverse" else (negative[0]["event"] if negative else ""),
            "horizonH": rules.get("horizonH", 48), "invalidation": float(inv),
            "trend": trend_map.get((self.shapes.get(symbol) or {}).get("label"), "unclear"),
            "summary": answer.get("summary", ""), "price": f.price,
            "drivers": drivers, "risks": [c["event"] for c in negative], "numbers": [],
            "context": ctx, "actionModifier": mod, "keyReason": answer.get("key_reason", ""), "catalysts": cats,
            "playbook": rules.get("playbook"), "riskCharacter": rules.get("riskCharacter"),
            "citations": answer.get("citations", []), "usage": answer.get("usage", {}), "trigger": trigger,
            "shapeAtResearch": (self.shapes.get(symbol) or {}).get("label"),
            "rulesStanceAtResearch": q_stance,
        }

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
                "recommendation": s["recommendation"], "detectedAt": s["detected_at"] // 1000,
                "playbook": p.get("playbook"), "riskCharacter": p.get("riskCharacter"), "timeframe": s["timeframe"],
                "horizonH": s["horizon_h"], "checks": p.get("checks", []), "research": r,
                "researchState": research_state, "researchAgeS": age_s, "staleReasons": reasons, "pinned": bool(p.get("pinned")),
                "researchTrigger": (r or {}).get("trigger"), "liveSignal": (self.rules.get(sym) or {}).get("stance", "flat")}

    async def llm_usage(self, user_id: int | None = None) -> dict:
        """Today's model calls for this user with token counts and an estimated cost, newest first."""
        day_start = (now_ms() // 86_400_000) * 86_400_000
        rows = await self.store.rows("analyses", "time >= ? AND user_id IS ?", (day_start, user_id), order="time DESC")
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
    async def payload(self, user_id: int | None = None) -> dict:
        """The Analysis tab for one user: shared scanner output, plus that user's research,
        positions and usage. Nothing private of anyone else's is in here."""
        analyses = {a["symbol"]: a for a in await self.store.latest_analyses(user_id)}
        setups = {sym: await self.desk.setup_for_user(s, user_id) for sym, s in (await self.desk.active_setups()).items()} if self.desk else {}
        jobs = self.jobs_for(user_id)
        held = {}
        if self.desk:
            for t in await self.desk.trades_for_user(user_id, "state IN ('waiting','ready','open')"):
                held.setdefault(t["symbol"], []).append({"tradeId": t["id"], "accountId": t["account_id"], "state": t["state"],
                                                          "side": t["side"], "status": t["status"],
                                                          "unrealizedUsd": self.desk._unrealized(t) if t["state"] == "open" else None,
                                                          "unrealizedR": (self.desk._unrealized(t) / t["risk_usd"]) if t["state"] == "open" and t["risk_usd"] else None})
        return {
            "desk": await self.desk.briefing(user_id) if self.desk else None,
            "lastScan": self.last_scan_ms // 1000, "scanIntervalS": config.SCAN_INTERVAL_S,
            "nextScan": (self.last_scan_ms // 1000 + config.SCAN_INTERVAL_S) if self.last_scan_ms else None,
            "scanStats": self.scan_stats, "research": self.jobs_summary(user_id),
            "warming": sorted(s for s in self.warming if s not in self.features),
            "llm": {**self.judge.status, "callsToday": await self.store.analyses_today(user_id), "dailyCap": None,
                    "topMovers": config.TOP_MOVERS, "ttlH": config.LLM_RESEARCH_TTL_S // 3600, "usage": await self.llm_usage(user_id)},
            "movers": [{**m, "rules": self.rules.get(m["symbol"]), "model": analyses.get(m["symbol"]),
                        "shape": self.shapes.get(m["symbol"]), "slippage": self.slippage.get(m["symbol"]),
                        "setup": self._setup_wire(setups.get(m["symbol"])), "held": held.get(m["symbol"], []),
                        "researchJob": jobs.get(m["symbol"])} for m in self.movers],
            "breadth": self.breadth, "tracker": await self._tracker(), "dvol": self.store.dvol,
            "alerts": {**(self.alerts.status() if self.alerts else {"configured": False}), "recent": list(self.alerts.recent)[:30] if self.alerts else []},
            "dailyLossBudgetPct": config.DAILY_LOSS_BUDGET_PCT, "accountSize": config.ACCOUNT_SIZE_USD,
        }
