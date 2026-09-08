"""The desk: setups (Wick's opinion) and trades (what an account did about it).

Two separate workflows that connect at BUILD TRADE:

  setup:  SCANNED -> RESEARCHED -> (recommendation enter | wait | pass) -> PASSED / EXPIRED
  trade:  WAITING -> READY -> OPEN -> CLOSED           (or OPEN straight away, or CANCELLED)

Rules of the house:
  - An unresearched setup can be PROMISING or WEAK but never says ENTER. Its button is RESEARCH.
  - A WAITING trade never opens itself. Wick watches the trigger every minute and marks the
    trade READY; opening is your click.
  - A stop is an order you placed, so it fills automatically (STOP FILLED), like a real prop
    account. A target is flagged (TARGET REACHED); closing is your click.
  - Low R:R is a PASS recommendation, still takeable under Advanced. Account rules hard-block.
"""
import logging
import math

from . import config
from .flow import slippage_table
from .indicators import atr, sma
from .planner import PLANNER_VERSION, RISK_PROFILES, build_plan, size_for
from .shape import SQRT24
from .store import Store
from .timeutil import now_ms

log = logging.getLogger(__name__)

H_MS = 3_600_000
FEE = 0.001                                      # taker, per side
ACTIVE_SETUP = ("scanned", "researched")
OPEN_LIKE = ("waiting", "ready", "open")
DOWN_SHAPES = {"trend_down", "flag_down", "capitulation", "blowoff_up"}   # against a long
UP_SHAPES = {"trend_up", "flag_up", "blowoff_up", "v_reversal", "capitulation"}  # against a short


def quality_of(rules: dict) -> tuple[str, str]:
    """STRONG / MIXED / WEAK from the share of passing checks, plus the first failing check."""
    checks = [c for c in rules.get("checks", []) if c["ok"] is not None]
    if not checks:
        return "weak", "not enough history"
    share = sum(1 for c in checks if c["ok"]) / len(checks)
    concern = next((c["name"] for c in checks if c["ok"] is False), "none")
    return ("strong" if share >= 0.85 else "mixed" if share >= 0.5 else "weak"), concern


class Desk:
    def __init__(self, store: Store, analysis, alerts=None, track_fn=None):
        self.store = store
        self.analysis = analysis
        self.alerts = alerts
        self.track_fn = track_fn          # async (symbol) -> None: start tracking a symbol traded manually
        self.reconciled = False           # set by reconcile_after_downtime(); trade actions wait for it

    # ---- setups ----------------------------------------------------------------------
    async def refresh_setups(self):
        """Called at the end of every scan. Creates or updates one active setup per symbol
        that is a top mover or has a non-flat rules stance; expires stale ones."""
        now = now_ms()
        top = {m["symbol"] for m in self.analysis.movers[: config.TOP_MOVERS]}
        for s in await self.store.rows("setups", "state IN ('scanned','researched') AND expires_at < ?", (now,)):
            await self.store.update("setups", s["id"], {"state": "expired", "updated_at": now})
        active = {s["symbol"]: s for s in await self.store.rows("setups", "state IN ('scanned','researched')")}
        for m in self.analysis.movers:
            sym = m["symbol"]
            rules = self.analysis.rules.get(sym) or {}
            if sym not in top and rules.get("stance", "flat") == "flat" and sym not in active:
                continue
            quality, concern = quality_of(rules)
            bias = {"up": "long", "down": "short"}.get(rules.get("trend"), "neutral")
            shape = self.analysis.shapes.get(sym) or {}
            payload = {"features": m["features"], "score": m["score"], "checks": rules.get("checks", []),
                       "shape": shape.get("label"), "shapeName": shape.get("name"),
                       "mfe48h": shape.get("mfe48h"), "rulesStance": rules.get("stance")}
            if sym in active:
                s = active[sym]
                await self.store.update("setups", s["id"], {"quality": quality, "concern": concern, "updated_at": now,
                                                            "bias": s["bias"] if s["state"] == "researched" else bias,
                                                            "payload": {**s["payload"], **payload}})
            else:
                await self.store.insert("setups", {
                    "symbol": sym, "detected_at": now, "timeframe": config.CONDITION_INTERVAL, "horizon_h": config.REC_HORIZON_H,
                    "state": "scanned", "quality": quality, "bias": bias, "concern": concern, "recommendation": None,
                    "updated_at": now, "expires_at": now + 2 * config.REC_HORIZON_H * H_MS, "payload": payload})
        # A model analysis newer than the setup (and newer than any "clear research") counts as
        # research, whoever triggered it.
        for s in await self.store.rows("setups", "state='scanned'"):
            a = await self.store.latest_analysis(s["symbol"])
            if a and a["time"] >= s["detected_at"] and a["time"] > s["payload"].get("researchClearedAt", -1):
                await self._attach_research(s, a)

    async def pin_setup(self, symbol: str) -> dict:
        """Move a watched coin into the research queue by hand. A pinned setup stays active
        regardless of the scanner's opinion until you dismiss it."""
        now = now_ms()
        active = await self.active_setups()
        if symbol in active:
            s = active[symbol]
            await self.store.update("setups", s["id"], {"updated_at": now, "expires_at": now + 30 * 86_400_000,
                                                        "payload": {**s["payload"], "pinned": True}})
            return await self.store.row("setups", s["id"])
        m = next((x for x in self.analysis.movers if x["symbol"] == symbol), None)
        if m is None:
            raise KeyError(f"{symbol} is not tracked yet")
        rules = self.analysis.rules.get(symbol) or {}
        quality, concern = quality_of(rules)
        shape = self.analysis.shapes.get(symbol) or {}
        sid = await self.store.insert("setups", {
            "symbol": symbol, "detected_at": now, "timeframe": config.CONDITION_INTERVAL, "horizon_h": config.REC_HORIZON_H,
            "state": "scanned", "quality": quality, "bias": {"up": "long", "down": "short"}.get(rules.get("trend"), "neutral"),
            "concern": concern, "recommendation": None, "updated_at": now, "expires_at": now + 30 * 86_400_000,
            "payload": {"features": m["features"], "score": m["score"], "checks": rules.get("checks", []), "shape": shape.get("label"),
                        "shapeName": shape.get("name"), "mfe48h": shape.get("mfe48h"), "rulesStance": rules.get("stance"),
                        "pinned": True, "researchClearedAt": now}})
        return await self.store.row("setups", sid)

    async def clear_research(self, setup_id: int | None = None):
        """Back to NOT RUN. Removes the cached model conclusion from the active setup(s) only:
        market history, trades, accounts and the usage log are untouched."""
        now = now_ms()
        where, params = ("id=?", (setup_id,)) if setup_id else ("state IN ('scanned','researched')", ())
        for s in await self.store.rows("setups", where, params):
            payload = {k: v for k, v in s["payload"].items() if k != "research"}
            payload["researchClearedAt"] = now
            await self.store.update("setups", s["id"], {"state": "scanned" if s["state"] in ("scanned", "researched") else s["state"],
                                                        "recommendation": None, "updated_at": now, "payload": payload})

    async def reset_workspace(self):
        """Development helper: expire every active setup so the next scan starts from square one.
        Keeps candles, accounts, trades and the model-usage log."""
        now = now_ms()
        for s in await self.store.rows("setups", "state IN ('scanned','researched')"):
            await self.store.update("setups", s["id"], {"state": "expired", "updated_at": now})
        await self.analysis.scan()

    async def _attach_research(self, s: dict, a: dict):
        rec = "pass" if a["stance"] == "flat" else a.get("action", "enter")
        bias = a["stance"] if a["stance"] != "flat" else s["bias"]
        await self.store.update("setups", s["id"], {
            "state": "researched", "recommendation": rec, "bias": bias, "updated_at": now_ms(),
            "payload": {**s["payload"], "research": {k: a.get(k) for k in ("time", "summary", "thesisVerdict", "mainRisk", "confidence",
                                                                            "trend", "horizonH", "invalidation", "drivers", "risks", "model",
                                                                            "price", "trigger", "shapeAtResearch", "rulesStanceAtResearch", "action")}}})

    async def research(self, setup_id: int) -> dict:
        s = await self.store.row("setups", setup_id)
        if not s:
            raise KeyError("setup not found")
        a = await self.analysis.maybe_research(s["symbol"], force=True)
        if a is None:
            raise RuntimeError(self.analysis.judge.status.get("error") or "model judge unavailable")
        await self._attach_research(s, a)
        return await self.store.row("setups", setup_id)

    async def pass_setup(self, setup_id: int):
        await self.store.update("setups", setup_id, {"state": "passed", "updated_at": now_ms()})

    async def active_setups(self) -> dict[str, dict]:
        return {s["symbol"]: s for s in await self.store.rows("setups", "state IN ('scanned','researched')")}

    # ---- plan / ticket ---------------------------------------------------------------
    def _market(self, symbol: str) -> dict:
        h1 = [c for c in self.store.ring(symbol, "1h") if c.closed]
        closes = [c.close for c in h1]
        a = atr(h1, 14)[-1] if len(h1) >= 15 else None
        t = self.store.tickers.get(symbol)
        if t is None and not closes:
            raise KeyError(f"no price for {symbol}")
        return {"h1": h1, "price": t.last if t else closes[-1], "unit": (a or 0) * SQRT24,
                "ma20": sma(closes, 20)[-1] if len(closes) >= 20 else None}

    def _fallback_plan(self, symbol: str, side: str, price: float):
        """A manual trade on a coin with no local history yet: 5% volatility stop, no target.
        Wick says PASS (it cannot judge), the trader decides."""
        from .planner import Plan
        d = 1 if side == "long" else -1
        return Plan(side, "enter", price, None, price * (1 - d * 0.05), None, None, None, "pass",
                    "No local history for this coin yet: default 5% stop, no target. Wick cannot judge this one.")

    async def plan_for(self, symbol: str, side: str, action: str, account_id: int, risk_profile: str,
                       setup: dict | None = None) -> dict:
        """Plan for a setup (guided) or a bare symbol+side (manual). Same ticket either way."""
        acct = await self.account_view(account_id)
        if not acct:
            raise KeyError("account not found")
        mk = self._market(symbol)
        if len(mk["h1"]) >= 200 and mk["unit"]:
            slip = None
            table = self.analysis.slippage.get(symbol)
            if table:
                row = next((r for r in table["rows"] if r["notional"] == config.ACCOUNT_SIZE_USD), table["rows"][0])
                slip = row["buyBps"] if side == "long" else row["sellBps"]
            shape = (setup or {}).get("payload", {}).get("shape") or (self.analysis.shapes.get(symbol) or {}).get("label")
            mfe_map = (setup or {}).get("payload", {}).get("mfe48h") or (self.analysis.shapes.get(symbol) or {}).get("mfe48h") or {}
            plan = build_plan(mk["h1"], side, action, mk["price"], mk["unit"], mk["ma20"], shape, mfe_map.get(side), slip)
        else:
            plan = self._fallback_plan(symbol, side, mk["price"])
        sizing = size_for(plan, acct["equity"], RISK_PROFILES[risk_profile], acct["openRiskUsd"], acct["dailyLossRemainingUsd"])
        return {"setup": setup, "symbol": symbol, "plan": plan.to_wire(), "sizing": sizing, "account": acct,
                "riskProfiles": RISK_PROFILES, "advisory": plan.recommendation == "pass",
                "researched": bool(setup) and setup["state"] == "researched", "manual": setup is None}

    async def plan(self, setup_id: int, account_id: int, risk_profile: str) -> dict:
        s = await self.store.row("setups", setup_id)
        if not s:
            raise KeyError("setup not found")
        side = s["bias"] if s["bias"] in ("long", "short") else "long"
        action = s["recommendation"] if s["recommendation"] in ("enter", "wait") else "enter"
        return await self.plan_for(s["symbol"], side, action, account_id, risk_profile, setup=s)

    async def create_trade(self, setup_id: int | None, account_id: int, risk_profile: str, overrides: dict | None, force: bool,
                           symbol: str | None = None, side: str | None = None) -> dict:
        """Guided (setup_id) or manual (symbol + side). Wick's PASS is advice and never blocks;
        only the account's risk rules do."""
        if setup_id:
            p = await self.plan(setup_id, account_id, risk_profile)
            s = p["setup"]
            symbol = s["symbol"]
        else:
            if not symbol or side not in ("long", "short"):
                raise ValueError("manual trade needs symbol and side")
            symbol = symbol.upper()
            if self.track_fn and symbol not in self.analysis.tracked_fn():
                await self.track_fn(symbol)          # start streaming it; history arrives in the background
            p = await self.plan_for(symbol, side, "enter", account_id, risk_profile)
            s = None
        plan, sizing = p["plan"], p["sizing"]
        # Wick's advice (PASS, or "not researched yet") is a soft gate: the UI's TAKE IT ANYWAY
        # sends force=True and proceeds. Only the account rules below are hard blocks.
        if not force and (plan["recommendation"] == "pass" or (s is not None and s["state"] != "researched")):
            why = plan["reason"] if plan["recommendation"] == "pass" else "Wick has not researched this setup yet."
            raise PermissionError(f"Wick advises against this trade: {why} Use TAKE IT ANYWAY to proceed.")
        ov = overrides or {}
        market_now = bool(ov.get("marketNow")) and plan["action"] == "wait"
        default_entry = self._market(symbol)["price"] if market_now else plan["entry"]   # "open now" ignores the trigger level
        entry, stop = float(ov.get("entry", default_entry)), float(ov.get("stop", plan["invalidation"]))
        target = ov.get("target", plan["target"])
        target = float(target) if target not in (None, "", 0) else None       # targets are optional; you may manage it yourself
        size = float(ov.get("sizeUsd", sizing["notionalUsd"]))
        direction = 1 if plan["side"] == "long" else -1
        if direction * (entry - stop) <= 0:
            raise ValueError("stop must be on the losing side of the entry")
        risk_usd = size * direction * (entry - stop) / entry
        acct = p["account"]
        blocks = []
        if acct["openRiskUsd"] + risk_usd > acct["equity"] * sizing["openRiskCapPct"] / 100 + 1e-9:
            blocks.append("portfolio open-risk cap")
        if risk_usd > acct["dailyLossRemainingUsd"] + 1e-9:
            blocks.append("daily loss budget")
        if acct["breached"]:
            blocks.append("account rules already breached")
        if blocks:
            raise PermissionError("ACCOUNT RISK LIMIT: " + ", ".join(blocks))
        now = now_ms()
        waiting = plan["action"] == "wait" and not ov.get("marketNow")
        horizon = s["horizon_h"] if s else config.REC_HORIZON_H
        feats = (s["payload"].get("features") if s else None) or (self.analysis.features.get(symbol).to_wire() if self.analysis.features.get(symbol) else None)
        row = {
            "account_id": account_id, "setup_id": setup_id or 0, "symbol": symbol, "side": plan["side"],
            "state": "waiting" if waiting else "open", "planner_version": PLANNER_VERSION, "risk_profile": risk_profile,
            "plan_entry": entry, "trigger_kind": plan["trigger_kind"] if waiting else None, "stop": stop,
            "target": target, "size_usd": size, "risk_usd": risk_usd,
            "horizon_h": horizon, "created_at": now, "expires_at": now + horizon * H_MS,
            "opened_at": None if waiting else now, "entry": None if waiting else entry,
            "status": "waiting" if waiting else "hold", "status_note": "Watching for the entry trigger." if waiting else "Position open. No action required.",
            "payload": {"plan": plan, "sizing": sizing, "snapshotEntry": feats,
                        "shapeAtEntry": (s["payload"].get("shape") if s else None) or (self.analysis.shapes.get(symbol) or {}).get("label"),
                        "funding": (feats or {}).get("fundingRate"),
                        "thesis": ((s["payload"].get("research") or {}).get("summary") if s else None) or "Manual trade.",
                        "wickSaid": plan["recommendation"], "againstAdvice": plan["recommendation"] == "pass"},
        }
        tid = await self.store.insert("trades", row)
        if self.alerts:
            verb = "WAITING for entry" if waiting else "OPENED"
            await self.alerts.send(f"trade:{tid}:{row['state']}", f"🧾 {acct['name']}: {symbol} {plan['side'].upper()} {verb} at {entry:.6g}, stop {stop:.6g}", "trade")
        return await self.store.row("trades", tid)

    # ---- lifecycle -------------------------------------------------------------------
    async def open_now(self, trade_id: int) -> dict:
        t = await self.store.row("trades", trade_id)
        if not t or t["state"] not in ("waiting", "ready"):
            raise ValueError("trade is not waiting")
        price = self._market(t["symbol"])["price"]
        await self.store.update("trades", trade_id, {"state": "open", "opened_at": now_ms(), "entry": price,
                                                     "status": "hold", "status_note": "Position open. No action required."})
        return await self.store.row("trades", trade_id)

    async def cancel(self, trade_id: int):
        await self.store.update("trades", trade_id, {"state": "cancelled", "closed_at": now_ms(), "exit_reason": "cancelled"})

    async def close_preview(self, trade_id: int, fraction: float) -> dict:
        """What a (partial) close would realize right now, before you confirm it."""
        t = await self.store.row("trades", trade_id)
        if not t or t["state"] != "open":
            raise ValueError("trade is not open")
        fraction = min(1.0, max(0.0, fraction))
        px = self._market(t["symbol"])["price"]
        part = {**t, "size_usd": t["size_usd"] * fraction, "risk_usd": t["risk_usd"] * fraction}
        costs = self._pnl_breakdown(part, px, now_ms())
        return {"price": px, "fraction": fraction, "closeUsd": part["size_usd"], "realizedUsd": costs["net"], "costs": costs,
                "remainingUsd": t["size_usd"] - part["size_usd"], "remainingRiskUsd": t["risk_usd"] - part["risk_usd"],
                "realizedR": costs["net"] / t["risk_usd"] if t["risk_usd"] else None}

    async def close(self, trade_id: int, reason: str = "manual", price: float | None = None, fraction: float = 1.0,
                    at_ms: int | None = None) -> dict:
        """Close all or part of a position. A partial close books the closed slice as its own
        closed row (so history and stats stay honest) and shrinks the open position.
        `at_ms` timestamps a reconstructed fill (a stop that crossed while Wick was offline)."""
        t = await self.store.row("trades", trade_id)
        if not t or t["state"] != "open":
            raise ValueError("trade is not open")
        fraction = min(1.0, max(0.0, fraction))
        if fraction <= 0:
            raise ValueError("nothing to close")
        px = price if price is not None else self._market(t["symbol"])["price"]
        now = at_ms if at_ms is not None else now_ms()
        if fraction < 0.999:
            part = {**t, "size_usd": t["size_usd"] * fraction, "risk_usd": t["risk_usd"] * fraction}
            costs = self._pnl_breakdown(part, px, now)
            row = {k: v for k, v in part.items() if k != "id"}
            row.update({"state": "closed", "closed_at": now, "exit": px, "exit_reason": "partial", "pnl_usd": costs["net"],
                        "r_multiple": costs["net"] / t["risk_usd"] if t["risk_usd"] else None, "status": "closed",
                        "status_note": f"Partial close ({fraction * 100:.0f}%).",
                        "payload": {**t["payload"], "costs": costs, "partialOf": t["id"], "fraction": fraction}})
            await self.store.insert("trades", row)
            partials = list(t["payload"].get("partials", [])) + [{"time": now, "fraction": fraction, "price": px, "pnl": costs["net"]}]
            await self.store.update("trades", trade_id, {"size_usd": t["size_usd"] - part["size_usd"], "risk_usd": t["risk_usd"] - part["risk_usd"],
                                                         "payload": {**t["payload"], "partials": partials}})
            if self.alerts:
                await self.alerts.send(f"trade:{trade_id}:partial:{now}", f"◔ {t['symbol']} {t['side']} reduced {fraction * 100:.0f}% at {px:.6g}: {costs['net']:+.0f} USD realized", "close")
            return await self.store.row("trades", trade_id)
        costs = self._pnl_breakdown(t, px, now)
        pnl = costs["net"]
        await self.store.update("trades", trade_id, {"state": "closed", "closed_at": now, "exit": px, "exit_reason": reason,
                                                     "pnl_usd": pnl, "r_multiple": pnl / t["risk_usd"] if t["risk_usd"] else None,
                                                     "status": "closed", "status_note": f"Closed ({reason}).",
                                                     "payload": {**t["payload"], "costs": costs}})
        if self.alerts:
            await self.alerts.send(f"trade:{trade_id}:closed", f"⏹ {t['symbol']} {t['side']} closed ({reason}) at {px:.6g}: {pnl:+.0f} USD", "close")
        return await self.store.row("trades", trade_id)

    def _pnl_breakdown(self, t: dict, price: float, at_ms: int) -> dict:
        """Being right about direction is not the same as keeping that amount:
        gross move, minus taker fees both ways, minus (or plus) funding while held."""
        direction = 1 if t["side"] == "long" else -1
        gross = direction * (price / t["entry"] - 1) * t["size_usd"]
        fees = 2 * FEE * t["size_usd"]
        rate = t["payload"].get("funding") or 0.0
        hours = (at_ms - t["opened_at"]) / H_MS
        funding = rate * (hours / 8) * direction * t["size_usd"]
        # Slippage is already inside the entry price (the planner shifts it by the book walk).
        return {"gross": gross, "fees": -fees, "funding": -funding, "hoursHeld": hours, "net": gross - fees - funding}

    def _pnl(self, t: dict, price: float, at_ms: int) -> float:
        return self._pnl_breakdown(t, price, at_ms)["net"]

    async def research_position(self, trade_id: int) -> dict:
        """One manual model call about an open position: what changed since entry, and what
        the holder should do now. Advisory only; nothing is closed or resized."""
        t = await self.store.row("trades", trade_id)
        if not t or t["state"] != "open":
            raise ValueError("trade is not open")
        sym = t["symbol"]
        f = self.analysis.features.get(sym)
        px = self._market(sym)["price"]
        now = now_ms()
        costs = self._pnl_breakdown(t, px, now)
        pack = {
            "symbol": sym, "side": t["side"], "openedUtcHoursAgo": round((now - t["opened_at"]) / H_MS, 1),
            "entry": t["entry"], "stop": t["stop"], "target": t["target"], "currentPrice": px,
            "unrealizedUsd": round(costs["net"], 2), "unrealizedR": round(costs["net"] / t["risk_usd"], 2) if t["risk_usd"] else None,
            "distanceToStopR": round(((px - t["stop"]) if t["side"] == "long" else (t["stop"] - px)) / max(1e-9, abs(t["entry"] - t["stop"])), 2),
            "monitorStatus": t["status"], "monitorNote": t["status_note"], "originalThesis": t["payload"].get("thesis"),
            "plan": t["payload"].get("plan"), "conditionsAtEntry": t["payload"].get("snapshotEntry"),
            "conditionsNow": f.to_wire() if f else None, "whatChangedLocally": self._what_changed(t),
            "shapeNow": (self.analysis.shapes.get(sym) or {}).get("name"), "shapeAtEntry": t["payload"].get("shapeAtEntry"),
        }
        answer = await self.analysis.judge.analyze_position(pack)
        row = {"symbol": f"{sym}#pos{trade_id}", "time": now, "model": answer.get("model") or self.analysis.judge.model,
               "stance": t["side"], "confidence": answer["confidence"], "horizonH": t["horizon_h"], "invalidation": t["stop"],
               "trend": answer["action"], "summary": answer["summary"], "price": px, "usage": answer.get("usage", {}),
               "trigger": "position", "kind": "position"}
        await self.store.insert_analysis(row)
        research = {"time": now, "action": answer["action"], "reason": answer["reason"], "whatChanged": answer["what_changed"],
                    "summary": answer["summary"], "risks": answer.get("risks", []), "drivers": answer.get("drivers", []),
                    "confidence": answer["confidence"], "price": px, "model": row["model"]}
        await self.store.update("trades", trade_id, {"payload": {**t["payload"], "positionResearch": research}})
        return research

    # ---- notes, accounts, ledger --------------------------------------------------
    async def set_notes(self, trade_id: int, notes: str):
        await self.store.update("trades", trade_id, {"notes": notes[:4000]})

    async def rename_account(self, account_id: int, name: str):
        await self.store.update("accounts", account_id, {"name": name.strip()[:60] or "Account"})

    async def delete_account(self, account_id: int):
        """Removes the account with its trades and equity history. Refuses to delete the last one."""
        if len(await self.store.rows("accounts")) <= 1:
            raise PermissionError("Keep at least one account.")
        for table in ("trades", "equity_snapshots"):
            await self.store._db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        await self.store._db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        await self.store._db.commit()

    async def ledger_csv(self, account_id: int) -> str:
        """Every trade the account ever placed, one row each, with costs and your notes."""
        import csv
        import io
        from datetime import datetime, timezone
        acct = await self.store.row("accounts", account_id)
        rows = await self.store.rows("trades", "account_id=?", (account_id,), order="created_at ASC")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["account", "trade_id", "symbol", "side", "state", "created_utc", "opened_utc", "closed_utc", "hours_held",
                    "planned_entry", "entry", "stop", "target", "exit", "exit_reason", "size_usd", "risk_usd", "risk_profile",
                    "gross_usd", "fees_usd", "funding_usd", "net_pnl_usd", "r_multiple", "wick_said", "against_advice",
                    "shape_at_entry", "planner", "thesis", "notes"])
        ts = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else ""
        for t in rows:
            p = t["payload"]
            c = p.get("costs") or {}
            w.writerow([acct["name"] if acct else account_id, t["id"], t["symbol"], t["side"], t["state"], ts(t["created_at"]),
                        ts(t["opened_at"]), ts(t["closed_at"]), f"{c.get('hoursHeld', 0):.1f}" if c else "",
                        t["plan_entry"], t["entry"], t["stop"], t["target"], t["exit"], t["exit_reason"],
                        round(t["size_usd"], 2), round(t["risk_usd"], 2), t["risk_profile"],
                        round(c["gross"], 2) if c else "", round(c["fees"], 2) if c else "", round(c["funding"], 2) if c else "",
                        round(t["pnl_usd"], 2) if t["pnl_usd"] is not None else "", round(t["r_multiple"], 3) if t["r_multiple"] is not None else "",
                        p.get("wickSaid"), p.get("againstAdvice"), p.get("shapeAtEntry"), t["planner_version"],
                        (p.get("thesis") or "").replace("\n", " "), (t.get("notes") or "").replace("\n", " ")])
        return buf.getvalue()

    async def snapshot_equity(self):
        """One equity point per account per minute, for the account curve."""
        t = (now_ms() // 60_000) * 60_000
        for a in await self.store.rows("accounts"):
            v = await self.account_view(a["id"])
            if v:
                await self.store._db.execute("INSERT OR REPLACE INTO equity_snapshots(account_id, time, equity) VALUES(?,?,?)",
                                             (a["id"], t, v["equity"]))
        await self.store._db.commit()

    async def equity_curve(self, account_id: int, rng: str) -> list[dict]:
        spans = {"1d": 86_400_000, "1w": 7 * 86_400_000, "1m": 30 * 86_400_000}
        since = now_ms() - spans[rng] if rng in spans else 0
        async with self.store._db.execute(
            "SELECT time, equity FROM equity_snapshots WHERE account_id=? AND time>=? ORDER BY time", (account_id, since)) as cur:
            rows = await cur.fetchall()
        # Thin to ~400 points so the SVG stays light.
        step = max(1, len(rows) // 400)
        return [{"time": r[0] // 1000, "equity": r[1]} for r in rows[::step]] + ([{"time": rows[-1][0] // 1000, "equity": rows[-1][1]}] if rows and step > 1 else [])

    # ---- replaying the tape after downtime -----------------------------------------
    async def _candles_since(self, symbol: str, since: int) -> tuple[list, str]:
        """Closed candles from `since` to now, finest resolution first, read from SQLite (the
        in-memory ring only holds the last ~25h of 1m bars). Where the 1m series does not reach
        back to `since`, the uncovered head comes from 5m, then 1h. Returns (bars, coarsest
        resolution used) so a reconstructed fill is labelled honestly. Each bar is
        (open_time, high, low, close, step_ms)."""
        steps = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}
        out, resolution, end = [], "1m", None
        for iv in ("1m", "5m", "1h"):
            sql = "SELECT open_time, h, l, c FROM candles WHERE symbol=? AND interval=? AND closed=1 AND open_time>=?"
            params: tuple = (symbol, iv, since)
            if end is not None:
                sql, params = sql + " AND open_time<?", params + (end,)
            async with self.store._db.execute(sql + " ORDER BY open_time", params) as cur:
                rows = await cur.fetchall()
            if rows:
                out += [(r[0], r[1], r[2], r[3], steps[iv]) for r in rows]
                resolution = iv
                end = rows[0][0]
            if end is not None and end <= since + steps[iv]:
                break                                   # covered back to the start
        return sorted(out), resolution

    async def reconcile_after_downtime(self, backfiller=None):
        """Called once at startup after history sync. Makes sure 1-minute candles exist for
        the whole life of every open or pending trade (pulling them from the exchange, which
        serves 1m history arbitrarily far back), then replays the tape with monitor()."""
        trades = await self.store.rows("trades", "state IN ('waiting','ready','open')")
        for t in trades:
            since = t["opened_at"] or t["created_at"]
            if backfiller is not None:
                try:
                    await backfiller.fetch_range(t["symbol"], "1m", since - 60_000, now_ms())
                except Exception:
                    log.exception("1m backfill for trade %d failed; replay will use coarser bars", t["id"])
        await self.monitor(replay=True)
        self.reconciled = True
        log.info("downtime reconciliation done for %d active trade(s)", len(trades))

    async def monitor(self, replay: bool = False):
        """Every minute: triggers for waiting trades, stops and flags for open ones.
        With replay=True (startup) the same logic runs over stored candles since each trade's
        open, so a stop crossed while Wick was offline fills at the candle when it crossed."""
        now = now_ms()
        try:
            await self.snapshot_equity()
        except Exception:
            log.exception("equity snapshot failed")
        for t in await self.store.rows("trades", "state IN ('waiting','ready','open')"):
            since = t["opened_at"] or t["created_at"]
            candles, resolution = await self._candles_since(t["symbol"], since)
            if not candles:
                continue
            hi, lo = max(c[1] for c in candles), min(c[2] for c in candles)
            direction = 1 if t["side"] == "long" else -1
            if t["state"] == "waiting":
                if t["expires_at"] and now > t["expires_at"]:
                    await self.store.update("trades", t["id"], {"state": "cancelled", "closed_at": now, "exit_reason": "expired",
                                                                "status": "expired", "status_note": "Entry never triggered within the horizon."})
                    continue
                touched = (lo <= t["plan_entry"] if direction > 0 else hi >= t["plan_entry"]) if t["trigger_kind"] == "pullback" \
                    else (hi >= t["plan_entry"] if direction > 0 else lo <= t["plan_entry"])
                if touched:
                    note = f"Entry trigger {t['plan_entry']:.6g} reached{' while Wick was offline' if replay else ''}. Conditions still valid. Ready to open."
                    await self.store.update("trades", t["id"], {"state": "ready", "status": "ready", "status_note": note})
                    if self.alerts:
                        await self.alerts.send(f"trade:{t['id']}:ready", f"✅ TRADE READY: {t['symbol']} {t['side']} reached {t['plan_entry']:.6g}", "ready")
            elif t["state"] == "open":
                # Walk the tape in order. The first candle that crosses the stop fills it at the stop
                # price, timestamped at that candle. If the same candle also reached the target, the
                # order inside the bar is unknowable: the conservative rule is that the adverse event
                # came first, and the fill is labelled ambiguous with the resolution used.
                stop_bar = next((c for c in candles if (c[2] <= t["stop"] if direction > 0 else c[1] >= t["stop"])), None)
                if stop_bar:
                    open_time, h, l, _, step = stop_bar
                    ambiguous = t["target"] is not None and ((h >= t["target"]) if direction > 0 else (l <= t["target"]))
                    await self.store.update("trades", t["id"], {"payload": {**t["payload"], "reconciliation": {
                        "resolution": resolution, "ambiguous": ambiguous, "replay": replay,
                        "rule": "stop assumed first when stop and target share a bar" if ambiguous else "stop crossed"}}})
                    await self.close(t["id"], "stop", t["stop"], at_ms=open_time + step)
                    continue
                price = self._market(t["symbol"])["price"]
                status, note = "hold", "Thesis intact. No action required."
                shape = (self.analysis.shapes.get(t["symbol"]) or {}).get("label")
                risk_per = direction * (t["entry"] - t["stop"])
                dist_stop = direction * (price - t["stop"]) / risk_per if risk_per else 1.0
                if t["target"] is not None and (direction * (price - t["target"]) >= 0):
                    status, note = "target_reached", f"Target {t['target']:.6g} reached. Review and close, or hold with a reason."
                elif dist_stop <= 0.25:
                    status, note = "stop_near", f"Price is within 0.25R of the stop {t['stop']:.6g}."
                elif shape and ((direction > 0 and shape in DOWN_SHAPES) or (direction < 0 and shape in UP_SHAPES)):
                    status, note = "shape_flipped", f"Structure changed: now '{shape}', against the position."
                elif t["opened_at"] + t["horizon_h"] * H_MS - now < 6 * H_MS:
                    status, note = "horizon_expiring", "Planned horizon ends within 6h. Decide whether the thesis still needs time."
                if status != t.get("status"):
                    await self.store.update("trades", t["id"], {"status": status, "status_note": note})
                    if status != "hold" and self.alerts:
                        await self.alerts.send(f"trade:{t['id']}:{status}", f"⚠ {t['symbol']} {t['side']}: {note}", "attention")

    # ---- views ---------------------------------------------------------------------
    def _unrealized(self, t: dict) -> float:
        try:
            return self._pnl(t, self._market(t["symbol"])["price"], now_ms())
        except KeyError:
            return 0.0            # price not streaming yet (freshly tracked manual trade)

    async def account_view(self, account_id: int) -> dict | None:
        acct = await self.store.row("accounts", account_id)
        if not acct:
            return None
        trades = await self.store.rows("trades", "account_id=?", (account_id,), order="created_at ASC")
        closed = [t for t in trades if t["state"] == "closed"]
        opens = [t for t in trades if t["state"] == "open"]
        equity, peak, curve = acct["size"], acct["size"], []
        for t in closed:
            equity += t["pnl_usd"]
            peak = max(peak, equity)
            curve.append({"time": t["closed_at"] // 1000, "equity": round(equity, 2)})
        unreal = sum(self._unrealized(t) for t in opens)
        day_start = (now_ms() // 86_400_000) * 86_400_000
        today = sum(t["pnl_usd"] for t in closed if t["closed_at"] >= day_start) + unreal
        daily_limit = acct["size"] * acct["daily_loss_pct"] / 100
        peak = max(peak, equity + unreal)      # equity-based drawdown includes open P&L, like a prop dashboard
        dd = (peak - (equity + unreal)) / peak * 100 if peak else 0.0
        wins = [t for t in closed if t["pnl_usd"] > 0]
        rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
        return {
            **acct, "equity": equity + unreal, "realized": equity, "unrealized": unreal,
            "returnPct": (equity + unreal - acct["size"]) / acct["size"] * 100,
            "targetUsd": acct["size"] * (1 + acct["target_pct"] / 100),
            "targetProgressPct": (equity + unreal - acct["size"]) / (acct["size"] * acct["target_pct"] / 100) * 100,
            "openRiskUsd": sum(t["risk_usd"] for t in opens), "openRiskPct": sum(t["risk_usd"] for t in opens) / max(equity, 1) * 100,
            "todayPnlUsd": today, "dailyLossRemainingUsd": max(0.0, daily_limit + min(0.0, today)),
            "drawdownPct": dd, "breached": today <= -daily_limit or dd >= acct["max_dd_pct"],
            "stats": {"n": len(closed), "winRate": len(wins) / len(closed) if closed else None,
                      "expectancyR": sum(rs) / len(rs) if rs else None, "maxDrawdownPct": self._max_dd(acct["size"], closed),
                      "curve": curve[-200:], "byShape": self._by_shape(closed)},
            "counts": {"open": len(opens), "waiting": sum(1 for t in trades if t["state"] == "waiting"),
                       "ready": sum(1 for t in trades if t["state"] == "ready"),
                       "attention": sum(1 for t in opens if t["status"] != "hold")},
        }

    @staticmethod
    def _max_dd(size, closed):
        eq, peak, worst = size, size, 0.0
        for t in closed:
            eq += t["pnl_usd"]
            peak = max(peak, eq)
            worst = max(worst, (peak - eq) / peak * 100)
        return worst

    @staticmethod
    def _by_shape(closed):
        out: dict[str, dict] = {}
        for t in closed:
            k = t["payload"].get("shapeAtEntry") or "unknown"
            d = out.setdefault(k, {"n": 0, "sumR": 0.0, "wins": 0})
            d["n"] += 1
            d["sumR"] += t["r_multiple"] or 0.0
            d["wins"] += t["pnl_usd"] > 0
        return out

    async def trades_view(self, account_id: int) -> dict:
        trades = await self.store.rows("trades", "account_id=?", (account_id,), order="created_at DESC", limit=300)
        for t in trades:
            if t["state"] == "open":
                try:
                    t["unrealizedUsd"] = self._unrealized(t)
                    t["currentPrice"] = self._market(t["symbol"])["price"]
                except KeyError:
                    t["unrealizedUsd"], t["currentPrice"] = 0.0, t["entry"]
                t["unrealizedR"] = t["unrealizedUsd"] / t["risk_usd"] if t["risk_usd"] else None
                t["changes"] = self._what_changed(t)
            for k in ("created_at", "expires_at", "opened_at", "closed_at"):
                if t.get(k):
                    t[k] = t[k] // 1000
        return {"open": [t for t in trades if t["state"] == "open"], "waiting": [t for t in trades if t["state"] in ("waiting", "ready")],
                "closed": [t for t in trades if t["state"] in ("closed", "cancelled")]}

    def _what_changed(self, t: dict) -> str:
        then = t["payload"].get("snapshotEntry") or {}
        f = self.analysis.features.get(t["symbol"])
        if not f or not then:
            return ""
        now = f.to_wire()
        bits = []
        v0, v1 = then.get("volMultiple"), now.get("volMultiple")
        if v0 and v1:
            bits.append("volume cooled" if v1 < 0.7 * v0 else "volume built" if v1 > 1.3 * v0 else "volume steady")
        f0, f1 = then.get("fundingRate"), now.get("fundingRate")
        if f0 is not None and f1 is not None:
            bits.append("funding normalized" if abs(f1) < abs(f0) * 0.6 else "funding stretched" if abs(f1) > abs(f0) * 1.6 else "funding steady")
        t0, t1 = then.get("takerBuyRatio24"), now.get("takerBuyRatio24")
        if t0 is not None and t1 is not None:
            bits.append("buyers pressing" if t1 > t0 + 0.05 else "sellers pressing" if t1 < t0 - 0.05 else "flow balanced")
        btc = self.store.tickers.get("BTCUSDT")
        if btc:
            bits.append(f"BTC {btc.change_pct:+.1f}% on the day")
        return "Since entry: " + ", ".join(bits) + "." if bits else ""

    async def briefing(self) -> dict:
        setups = await self.store.rows("setups", "state IN ('scanned','researched')")
        trades = await self.store.rows("trades", "state IN ('waiting','ready','open')")
        opens = [t for t in trades if t["state"] == "open"]
        return {"newSetups": sum(1 for s in setups if s["state"] == "scanned" and s["quality"] != "weak"),
                "researched": sum(1 for s in setups if s["state"] == "researched"),
                "waiting": sum(1 for t in trades if t["state"] == "waiting"), "ready": sum(1 for t in trades if t["state"] == "ready"),
                "open": len(opens), "attention": sum(1 for t in opens if t["status"] != "hold"),
                "openPnlUsd": sum(self._unrealized(t) for t in opens)}

    async def judges_tally(self) -> dict:
        """Rules / model calls in R (pnl over stop distance), for the you-vs-judges table."""
        out = {}
        for r in await self.store.recommendations(limit=5000):
            if r["closed_at"] is None or not r["invalidation"] or r["pnl_pct"] is None:
                continue
            risk = abs(r["entry"] - r["invalidation"]) / r["entry"]
            if risk <= 0:
                continue
            d = out.setdefault(r["source"], {"n": 0, "sumR": 0.0, "wins": 0})
            d["n"] += 1
            d["sumR"] += r["pnl_pct"] / risk
            d["wins"] += r["pnl_pct"] > 0
        return out

    async def ensure_default_account(self):
        if not await self.store.rows("accounts", limit=1):
            await self.store.insert("accounts", {"name": "$25K Challenge", "size": 25_000.0, "daily_loss_pct": 4.0,
                                                 "max_dd_pct": 8.0, "target_pct": 8.0, "created_at": now_ms()})
