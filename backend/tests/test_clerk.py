"""Multi-user Wick: Clerk verification, the middleware gate, provisioning, isolation, migration.
Tokens are signed with a throwaway RSA key and verified against a JWKS built from it, so
nothing here touches the network or needs a Clerk account."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app.auth import Auth, AuthMiddleware
from app.broadcaster import Broadcaster
from app.clerk import ClerkAuth
from app.desk import Desk
from app.store import Store
from tests.test_desk import FakeAnalysis, H, T0, world  # noqa: F401

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWKS = {"keys": [{**RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1", "alg": "RS256", "use": "sig"}]}
PARTY = "http://wick.test"


def token(sub="user_a", exp=None, azp=PARTY, kid="k1"):
    return jwt.encode({"sub": sub, "exp": exp or int(time.time()) + 60, "azp": azp}, KEY, algorithm="RS256", headers={"kid": kid})


async def test_verify_accepts_only_valid_unexpired_tokens_for_our_party():
    c = ClerkAuth("sk_test_x", [PARTY], jwks=JWKS)
    assert (await c.verify(token()))["sub"] == "user_a"
    assert await c.verify(token(exp=int(time.time()) - 30)) is None          # expired
    assert await c.verify(token(azp="http://evil.test")) is None             # wrong party
    assert await c.verify(token()[:-4] + "abcd") is None                     # tampered
    assert await c.verify("not.a.jwt") is None and await c.verify(None) is None
    assert await ClerkAuth(None).verify(token()) is None                     # disabled: nothing verifies
    assert ClerkAuth.token_from({"authorization": "Bearer abc"}, {}) == "abc"
    assert ClerkAuth.token_from({}, {"__session": "cookie-token"}) == "cookie-token"


def test_middleware_401s_without_a_clerk_session_and_stashes_sub():
    clerk = ClerkAuth("sk_test_x", [PARTY], jwks=JWKS)
    app = FastAPI()
    app.add_middleware(AuthMiddleware, auth=Auth(None, "s", secure_cookie=False), clerk=clerk)

    @app.get("/api/whoami")
    def whoami(request: Request):
        return {"sub": request.state.clerk_sub}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    c = TestClient(app, base_url=PARTY)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/whoami").status_code == 401
    assert c.get("/api/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/api/whoami", headers={"Authorization": f"Bearer {token('user_b')}"})
    assert r.status_code == 200 and r.json()["sub"] == "user_b"
    assert c.get("/api/whoami", cookies={"__session": token("user_c")}).json()["sub"] == "user_c"


async def test_provisioning_isolation_and_legacy_migration(world):  # noqa: F811
    store, analysis, desk = world
    # The fixture created one legacy account with no owner: the world before users.
    legacy = await store.rows("accounts")
    assert len(legacy) == 1 and legacy[0].get("user_id") is None

    jason, created = await store.get_or_create_user("user_jason", active=True)
    assert created and jason["access_status"] == "active"
    again, created = await store.get_or_create_user("user_jason", active=True)
    assert not created and again["id"] == jason["id"]                        # idempotent
    moved = await store.assign_legacy(jason["id"])
    assert moved["accounts"] == 1
    assert (await store.assign_legacy(jason["id"]))["accounts"] == 0            # nothing left to move

    other, _ = await store.get_or_create_user("user_other", active=False)
    assert other["access_status"] == "invited"                                 # beta: signed up, not let in
    await desk.ensure_default_account(other["id"])
    await desk.ensure_default_account(other["id"])                             # still exactly one
    mine = await desk.accounts_for(other["id"])
    assert len(mine) == 1 and mine[0]["size"] == 25_000 and mine[0]["user_id"] == other["id"]
    assert [a["id"] for a in await desk.accounts_for(jason["id"])] == [legacy[0]["id"]]   # Jason kept his, not the new one

    # Trades follow their account's owner.
    t = await desk.create_trade(None, legacy[0]["id"], "standard", None, force=True, symbol="X", side="long", user_id=jason["id"])
    assert [x["id"] for x in await desk.trades_for_user(jason["id"])] == [t["id"]]
    assert await desk.trades_for_user(other["id"]) == []

    # Research is per user on a shared setup: Jason's research does not make it researched for the other.
    await desk.refresh_setups()
    s = list((await desk.active_setups()).values())[0]
    await desk.research(s["id"], jason["id"], wait=True)
    assert (await desk.setup_for_user(s, jason["id"]))["state"] == "researched"
    assert (await desk.setup_for_user(s, other["id"]))["state"] == "scanned"
    assert await store.analyses_today(jason["id"]) >= 0 and await store.latest_analysis("X", other["id"]) is None
    # Clear Research hides it for Jason only and keeps the usage row.
    await desk.clear_research(jason["id"], s["id"])
    assert (await desk.setup_for_user(s, jason["id"]))["state"] == "scanned"
    assert len(await store.rows("analyses", "user_id=?", (jason["id"],))) == 1


async def test_private_events_reach_only_their_user():
    class Sock:
        async def send_json(self, m): pass
    b = Broadcaster(10, on_depth_wanted=lambda s: None)
    a, c = b.add(Sock()), b.add(Sock())
    a.user_id, c.user_id = 1, 2
    b.publish_user(1, {"type": "research", "symbol": "X"})
    assert a.queue.qsize() == 1 and c.queue.qsize() == 0
    b.publish_all({"type": "scan"})
    assert a.queue.qsize() == 2 and c.queue.qsize() == 1
