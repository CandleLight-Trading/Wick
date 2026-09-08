"""Shared-password authentication for a single-user deployment.

Why this exists: a public URL with no login means anyone who finds it can click Research
and spend the OpenAI budget. One password is enough for one user; what matters is doing the
small things right:

  - session = HMAC-signed token in an HttpOnly, SameSite=Lax cookie (Secure in production)
  - every /api route and the /ws socket require it; /healthz and /api/login do not
  - mutating requests must come from our own origin (CSRF)
  - the WebSocket checks Origin as well as the cookie
  - sessions expire; "log out everywhere" bumps an epoch that invalidates all tokens
  - login attempts are throttled per address, and the comparison is constant-time

Locally, with no WICK_PASSWORD set, auth is off and development is unchanged.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlparse

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

COOKIE = "wick_session"
SESSION_TTL_S = 30 * 86_400
PUBLIC_PATHS = ("/healthz", "/api/login", "/api/auth")     # everything else under /api needs a session
MAX_FAILURES = 5
FAILURE_WINDOW_S = 60


class Auth:
    def __init__(self, password: str | None, secret: str, secure_cookie: bool):
        self.epoch = 0                       # bump to revoke every session
        self._failures: dict[str, list[float]] = {}
        self.configure(password, secret, secure_cookie)

    def configure(self, password: str | None, secret: str, secure_cookie: bool):
        """The middleware needs the instance at app construction, before the database (and
        its persisted secret) is open, so the real settings arrive here from lifespan."""
        self.enabled = bool(password)
        self._password_hash = hashlib.sha256(password.encode()).digest() if password else b""
        self._secret = secret.encode()
        self.secure_cookie = secure_cookie

    # ---- tokens ------------------------------------------------------------------
    def issue(self) -> str:
        payload = json.dumps({"exp": int(time.time()) + SESSION_TTL_S, "epoch": self.epoch, "sid": secrets.token_hex(8)}).encode()
        sig = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload).decode() + "." + base64.urlsafe_b64encode(sig).decode()

    def verify(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        try:
            p64, s64 = token.split(".", 1)
            payload = base64.urlsafe_b64decode(p64.encode())
            sig = base64.urlsafe_b64decode(s64.encode())
        except Exception:
            return False
        if not hmac.compare_digest(hmac.new(self._secret, payload, hashlib.sha256).digest(), sig):
            return False
        data = json.loads(payload)
        return data.get("epoch") == self.epoch and data.get("exp", 0) > time.time()

    def check_password(self, given: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), self._password_hash)

    def revoke_all(self):
        self.epoch += 1

    # ---- throttling ------------------------------------------------------------------
    def throttled(self, addr: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(addr, []) if now - t < FAILURE_WINDOW_S]
        self._failures[addr] = recent
        return len(recent) >= MAX_FAILURES

    def record_failure(self, addr: str):
        self._failures.setdefault(addr, []).append(time.time())

    # ---- helpers ---------------------------------------------------------------------
    def set_cookie(self, resp: Response, token: str):
        resp.set_cookie(COOKIE, token, max_age=SESSION_TTL_S, httponly=True, samesite="lax", secure=self.secure_cookie, path="/")

    def clear_cookie(self, resp: Response):
        resp.delete_cookie(COOKIE, path="/")

    def session_ok(self, request_or_ws) -> bool:
        return not self.enabled or self.verify(request_or_ws.cookies.get(COOKIE))

    @staticmethod
    def same_origin(headers, host: str) -> bool:
        """Mutations and sockets must originate from our own page. Origin is set by browsers on
        every cross-site request; a missing Origin with a matching Referer is accepted too."""
        origin = headers.get("origin") or headers.get("referer")
        if not origin:
            return False
        return urlparse(origin).netloc.lower() == host.lower()


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth: Auth):
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not self.auth.enabled or not path.startswith("/api") or path.startswith(PUBLIC_PATHS):
            return await call_next(request)
        if not self.auth.session_ok(request):
            return JSONResponse({"detail": "login required"}, status_code=401)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not Auth.same_origin(request.headers, request.headers.get("host", "")):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)
