"""Who is asking, and what they may do (Admin or Viewer).

Off unless LR_AUTH_HEADER is set, e.g. to Cf-Access-Authenticated-User-Email; then that
header's value is the user. With LR_AUTH_TEAM_DOMAIN and LR_AUTH_AUD also set, the request
must carry Cloudflare Access's signed Cf-Access-Jwt-Assertion too: it's checked against the
team's public keys, and its email has to match the header, so the header can't be forged.

Users listed in LR_ADMINS are Admins; everyone else is a Viewer, who can look at everything
but change nothing (no cat corrections, robot actions, live view, clip downloads or keeps,
settings or logs). A request with no identity is an Admin when it didn't come through
Cloudflare (direct access on the LAN) and is refused when it did, so a Cloudflare hostname
or path without an Access policy never gets admin rights.

Everything is configured through environment variables, so the web UI can't loosen it."""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass

HEADER = os.environ.get("LR_AUTH_HEADER", "").strip()
TEAM = re.sub(r"^https?://", "", os.environ.get("LR_AUTH_TEAM_DOMAIN", "").strip()).rstrip("/")
AUD = os.environ.get("LR_AUTH_AUD", "").strip()
ADMINS = {e.strip().lower() for e in os.environ.get("LR_ADMINS", "").split(",") if e.strip()}
JWT_HEADER = "Cf-Access-Jwt-Assertion"

# what a Viewer may still send: asking for a sync is harmless
VIEWER_WRITES = {("POST", "/api/sync")}
# pages and data only Admins may see
ADMIN_READS = re.compile(r"^/(settings$|api/settings$|api/logs/|api/live/)")


@dataclass
class User:
    email: str | None  # None: no identity (auth off, or direct LAN access)
    role: str          # "admin" | "viewer"

    def json(self) -> dict:
        return {"email": self.email, "role": self.role}


class Denied(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


_jwks = None


def _verify(token: str) -> dict:
    """Claims of a Cloudflare Access JWT, checked against the team's signing keys."""
    global _jwks
    import jwt  # only needed when verification is configured
    if _jwks is None:
        _jwks = jwt.PyJWKClient(f"https://{TEAM}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
    key = _jwks.get_signing_key_from_jwt(token)
    return jwt.decode(token, key.key, algorithms=["RS256"], audience=AUD, issuer=f"https://{TEAM}",
                      options={"require": ["exp", "iss", "aud"]})  # as Cloudflare's own example does


def verifying() -> bool:
    return bool(TEAM and AUD)


def identify(headers) -> User:
    """The user for a request, from its headers. Raises Denied when it must be refused."""
    if not HEADER:
        return User(None, "admin")
    email = (headers.get(HEADER) or "").strip().lower()
    if not email:
        if headers.get("Cf-Ray"):  # came through Cloudflare, but Access didn't vouch for anyone
            raise Denied(401, f"Sign in through Cloudflare Access (no {HEADER} header).")
        return User(None, "admin")
    if TEAM or AUD:
        if not verifying():
            raise Denied(500, "Set both LR_AUTH_TEAM_DOMAIN and LR_AUTH_AUD to verify Cloudflare Access.")
        token = headers.get(JWT_HEADER)
        if not token:
            raise Denied(401, f"Missing {JWT_HEADER}; sign in through Cloudflare Access.")
        try:
            claims = _verify(token)
        except Exception as err:
            raise Denied(401, f"Cloudflare Access token rejected: {err}") from None
        if (claims.get("email") or "").lower() != email:
            raise Denied(401, f"{HEADER} doesn't match the signed Cloudflare Access token.")
    return User(email, "admin" if email in ADMINS else "viewer")


# The last request that got admin rights only because it came straight to the app (not
# through Cloudflare) while roles are on: Settings warns, since that means the port is open.
_direct: dict | None = None
_direct_lock = threading.Lock()


def note_direct(addr: str | None) -> None:
    global _direct
    if addr in ("127.0.0.1", "::1"):  # the container itself, e.g. its health check
        return
    with _direct_lock:
        _direct = {"at": time.time(), "addr": addr}


def allowed(user: User, method: str, path: str) -> bool:
    if user.role == "admin":
        return True
    if method not in ("GET", "HEAD", "OPTIONS"):
        return (method, path) in VIEWER_WRITES
    return not ADMIN_READS.match(path)


def describe() -> dict:
    """The access setup, for the Settings page."""
    return {"header": HEADER or None, "verify": verifying(), "team": TEAM or None,
            "admins": sorted(ADMINS), "direct": _direct if HEADER else None}
