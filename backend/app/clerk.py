"""Clerk session verification for multi-user Wick.

Clerk authenticates identity; Wick stays authoritative for every private row. A request
carries a Clerk session JWT (Authorization: Bearer, or the __session cookie Clerk sets on the
root domain). We verify it networklessly against Clerk's JWKS (fetched once, cached, refreshed
only when an unknown key id appears), check expiry and the authorized party, and hand back
the `sub` claim. Nothing else from the token is trusted; the user id supplied by any client
field is ignored.

Without CLERK_SECRET_KEY the class is disabled and the legacy shared-password auth applies,
so local development is unchanged.
"""
import logging
import time

import httpx
import jwt
from jwt import PyJWK

log = logging.getLogger(__name__)

JWKS_URL = "https://api.clerk.com/v1/jwks"
REFETCH_MIN_S = 60


class ClerkAuth:
    def __init__(self, secret_key: str | None, authorized_parties: list[str] | None = None, jwks: dict | None = None,
                 jason_user_id: str | None = None):
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self.configure(secret_key, authorized_parties, jason_user_id)
        if jwks:
            self._load(jwks)

    def configure(self, secret_key: str | None, authorized_parties: list[str] | None = None, jason_user_id: str | None = None):
        """The middleware needs the instance at app construction; the real settings arrive from lifespan."""
        self.enabled = bool(secret_key)
        self._secret = secret_key
        self.authorized_parties = [p.rstrip("/") for p in (authorized_parties or []) if p]
        self.jason_user_id = jason_user_id       # the one Clerk user who is active by default and inherits legacy data

    def _load(self, jwks: dict):
        self._keys = {k["kid"]: PyJWK(k) for k in jwks.get("keys", []) if k.get("kid")}
        self._fetched_at = time.time()

    async def refresh(self):
        if not self._secret or time.time() - self._fetched_at < REFETCH_MIN_S:
            return
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(JWKS_URL, headers={"Authorization": f"Bearer {self._secret}"})
            r.raise_for_status()
            self._load(r.json())
        log.info("clerk jwks loaded: %d key(s)", len(self._keys))

    async def verify(self, token: str | None) -> dict | None:
        """Claims if the token is a valid, unexpired Clerk session for one of our parties; else None."""
        if not self.enabled or not token:
            return None
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        if kid not in self._keys:
            try:
                await self.refresh()
            except Exception as e:
                log.warning("clerk jwks refresh failed: %r", e)
        key = self._keys.get(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(token, key.key, algorithms=["RS256"], options={"require": ["exp", "sub"]}, leeway=5)
        except jwt.PyJWTError:
            return None
        azp = claims.get("azp")
        if self.authorized_parties and azp and azp.rstrip("/") not in self.authorized_parties:
            return None
        return claims

    @staticmethod
    def token_from(headers, cookies) -> str | None:
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return cookies.get("__session")
