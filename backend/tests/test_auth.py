import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import COOKIE, Auth, AuthMiddleware


def test_tokens_sign_verify_expire_and_revoke(monkeypatch):
    a = Auth("hunter2", "secret", secure_cookie=False)
    tok = a.issue()
    assert a.verify(tok)
    assert not a.verify(tok[:-3] + "abc")               # tampered signature
    assert not a.verify(None) and not a.verify("garbage")
    a.revoke_all()
    assert not a.verify(tok)                            # epoch bumped: every session dies
    fresh = a.issue()
    monkeypatch.setattr(time, "time", lambda: time.time.__wrapped__() + 31 * 86_400 if hasattr(time.time, "__wrapped__") else 2e12)
    assert not a.verify(fresh)                          # expired


def test_password_and_throttle():
    a = Auth("hunter2", "secret", secure_cookie=False)
    assert a.check_password("hunter2") and not a.check_password("hunter3")
    for _ in range(5):
        a.record_failure("1.2.3.4")
    assert a.throttled("1.2.3.4") and not a.throttled("5.6.7.8")


def app_with_auth(password="pw"):
    auth = Auth(password, "secret", secure_cookie=False)
    app = FastAPI()
    app.add_middleware(AuthMiddleware, auth=auth)

    @app.get("/api/thing")
    def thing():
        return {"ok": True}

    @app.post("/api/thing")
    def mutate():
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/api/login")
    def login(body: dict):
        if auth.check_password(body.get("password", "")):
            from fastapi import Response
            r = Response(content=json.dumps({"ok": True}), media_type="application/json")
            auth.set_cookie(r, auth.issue())
            return r
        from fastapi import HTTPException
        raise HTTPException(401, "wrong password")

    return app, auth


def test_middleware_gates_api_but_not_health_and_checks_origin():
    app, auth = app_with_auth()
    c = TestClient(app, base_url="http://wick.test")
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/thing").status_code == 401
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    r = c.post("/api/login", json={"password": "pw"})
    assert r.status_code == 200 and COOKIE in r.cookies
    assert c.get("/api/thing").status_code == 200                                   # cookie carried
    assert c.post("/api/thing").status_code == 403                                   # no Origin: refused
    assert c.post("/api/thing", headers={"Origin": "http://evil.test"}).status_code == 403
    assert c.post("/api/thing", headers={"Origin": "http://wick.test"}).status_code == 200
    auth.revoke_all()
    assert c.get("/api/thing").status_code == 401                                   # logged out everywhere


def test_auth_disabled_when_no_password():
    app, _ = app_with_auth(password=None)
    c = TestClient(app)
    assert c.get("/api/thing").status_code == 200 and c.post("/api/thing").status_code == 200
