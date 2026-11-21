"""Access control: Admin / Viewer roles from Cloudflare Access, with the signed token checked."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import lr_auth

ADMIN, VIEWER = "alexis@example.com", "viewer@example.com"
TEAM, AUD = "myteam.cloudflareaccess.com", "aud-tag-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    """Stands in for https://<team>/cdn-cgi/access/certs."""
    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": KEY.public_key()})


def token(email, aud=AUD, iss=f"https://{TEAM}", exp=3600, key=KEY, drop=(), iat=None):
    claims = {"email": email, "aud": aud, "iss": iss, "exp": int(time.time()) + exp}
    if iat is not None:
        claims["iat"] = iat
    for k in drop:
        claims.pop(k)
    return jwt.encode(claims, key, algorithm="RS256")


def headers(email=None, cloudflare=True, tok=None):
    h = {}
    if email:
        h["Cf-Access-Authenticated-User-Email"] = email
    if cloudflare:
        h["Cf-Ray"] = "8abc-SYD"
    if tok:
        h["Cf-Access-Jwt-Assertion"] = tok
    return h


@pytest.fixture
def roles(env, monkeypatch):
    monkeypatch.setattr(lr_auth, "HEADER", "Cf-Access-Authenticated-User-Email")
    monkeypatch.setattr(lr_auth, "ADMINS", {ADMIN})
    env.robot()
    env.visit(3600)
    return env


@pytest.fixture
def verifying(roles, monkeypatch):
    monkeypatch.setattr(lr_auth, "TEAM", TEAM)
    monkeypatch.setattr(lr_auth, "AUD", AUD)
    monkeypatch.setattr(lr_auth, "_jwks", FakeJWKS())
    return roles


def statuses(env, h):
    event = env.db.execute("SELECT id FROM activities LIMIT 1").fetchone()["id"]
    c = env.client
    return {
        "view": c.get("/api/events", headers=h).status_code,
        "correct": c.post(f"/api/events/{event}/override", json={"note": "n"}, headers=h).status_code,
        "settings": c.get("/settings", headers=h).status_code,
        "robot": c.post("/api/robot/LR5-TEST-1/command", json={"command": "clean"}, headers=h).status_code,
        "health": c.get("/api/health", headers=h).status_code,
    }


def test_off_by_default_everyone_is_admin(env):
    env.robot()
    env.visit(3600)
    s = statuses(env, headers())
    assert s["correct"] == 200 and s["settings"] == 200


def test_viewer_can_look_but_not_change(roles):
    s = statuses(roles, headers(VIEWER))
    assert (s["view"], s["correct"], s["settings"], s["robot"], s["health"]) == (200, 403, 403, 403, 200)


def test_viewer_can_still_sync_and_get_reports(roles):
    pet = roles.pet("PET-1", "Ida")
    assert roles.client.post("/api/sync", headers=headers(VIEWER)).status_code in (202, 409, 429)
    assert roles.client.get(f"/report/{pet}.pdf", headers=headers(VIEWER)).status_code == 200


def test_admin_emails_match_case_insensitively(roles):
    assert statuses(roles, headers(ADMIN.upper()))["settings"] == 200


def test_no_identity_is_admin_on_the_lan_but_refused_via_cloudflare(roles):
    assert statuses(roles, headers(cloudflare=False))["settings"] == 200
    assert statuses(roles, headers(cloudflare=True))["view"] == 401


def test_direct_admin_access_is_reported_on_settings(roles):
    roles.client.get("/api/events", headers=headers(cloudflare=False), environ_base={"REMOTE_ADDR": "192.168.1.20"})
    access = roles.client.get("/api/settings", headers=headers(ADMIN)).get_json()["access"]
    assert access["direct"]["addr"] == "192.168.1.20"


def test_loopback_requests_are_not_reported_as_direct_access(roles):
    roles.client.get("/api/events", headers=headers(cloudflare=False))  # test client's address is 127.0.0.1
    assert roles.client.get("/api/settings", headers=headers(ADMIN)).get_json()["access"]["direct"] is None


def test_viewers_dont_see_the_settings_tab(roles):
    serial = "LR5-TEST-1"
    viewer = roles.client.get(f"/robots/{serial}", headers=headers(VIEWER)).data.decode()
    admin = roles.client.get(f"/robots/{serial}", headers=headers(ADMIN)).data.decode()
    assert 'href="/settings"' not in viewer and 'href="/settings"' in admin
    assert '"role": "viewer"' in viewer


def test_valid_token_is_accepted(verifying):
    assert statuses(verifying, headers(ADMIN, tok=token(ADMIN)))["settings"] == 200
    assert statuses(verifying, headers(VIEWER, tok=token(VIEWER)))["correct"] == 403


@pytest.mark.parametrize("case", ["no token", "other user's token", "wrong audience", "wrong issuer",
                                  "expired", "foreign key", "no expiry"])
def test_bad_tokens_are_refused(verifying, case):
    tok = {
        "no token": None,
        "other user's token": token(VIEWER),
        "wrong audience": token(ADMIN, aud="another-app"),
        "wrong issuer": token(ADMIN, iss="https://evil.cloudflareaccess.com"),
        "expired": token(ADMIN, exp=-60),
        "foreign key": token(ADMIN, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        "no expiry": token(ADMIN, drop=("exp",)),
    }[case]
    assert statuses(verifying, headers(ADMIN, tok=tok))["view"] == 401


def test_half_configured_verification_refuses(roles, monkeypatch):
    monkeypatch.setattr(lr_auth, "TEAM", TEAM)  # AUD missing
    assert statuses(roles, headers(ADMIN))["view"] == 500
