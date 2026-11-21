"""The access log: one line per session, and refused requests."""
import logging
import time

import pytest

import lr_auth
from test_auth import ADMIN, AUD, FakeJWKS, TEAM, VIEWER, headers, token

CF = {"Cf-Connecting-Ip": "203.0.113.7", "Cf-Ipcountry": "AU"}


@pytest.fixture
def log(env, caplog, monkeypatch):
    monkeypatch.setattr(lr_auth, "HEADER", "Cf-Access-Authenticated-User-Email")
    monkeypatch.setattr(lr_auth, "ADMINS", {ADMIN})
    caplog.set_level(logging.INFO, logger="lr_access")
    env.robot()
    env.visit(3600)
    return lambda: [r.getMessage() for r in caplog.records if r.name == "lr_access"]


def test_one_line_per_session_not_per_request(env, log):
    for _ in range(5):
        env.client.get("/api/events", headers={**headers(ADMIN), **CF})
    assert log() == ["Session started: alexis@example.com (Admin) from 203.0.113.7 [AU] via Cloudflare"]


def test_a_new_session_after_being_idle(env, log, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(lr_auth.time, "time", lambda: clock[0])
    env.client.get("/api/events", headers=headers(ADMIN))
    clock[0] += 10 * 60
    env.client.get("/api/events", headers=headers(ADMIN))
    clock[0] += lr_auth.SESSION_GAP_S + 1
    env.client.get("/api/events", headers=headers(ADMIN))
    assert len(log()) == 2


def test_cloudflare_sign_ins_are_the_sessions(env, log, monkeypatch):
    monkeypatch.setattr(lr_auth, "TEAM", TEAM)
    monkeypatch.setattr(lr_auth, "AUD", AUD)
    monkeypatch.setattr(lr_auth, "_jwks", FakeJWKS())
    now = int(time.time())
    morning, evening = token(ADMIN, iat=now - 8 * 3600), token(ADMIN, iat=now - 60)
    for tok in (morning, morning, evening, evening):  # two requests in each of two sign-ins
        env.client.get("/api/events", headers=headers(ADMIN, tok=tok))
    lines = log()
    assert len(lines) == 2 and all("signed in to Cloudflare Access at" in line for line in lines)


def test_direct_access_is_logged_by_address(env, log):
    env.client.get("/api/events", headers=headers(cloudflare=False), environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert log() == ["Session started: direct access (Admin, no identity) from 192.168.1.20 directly"]


def test_refusals_are_logged_once_per_repeat(env, log):
    for _ in range(3):
        env.client.post("/api/settings", json={"unit": "lb"}, headers={**headers(VIEWER), **CF})
    env.client.get("/api/events", headers=headers(cloudflare=True))  # through Cloudflare, no identity
    refused = [l for l in log() if l.startswith("Refused")]
    assert refused == [
        "Refused (403): viewer@example.com (Viewer) from 203.0.113.7 [AU]: POST /api/settings: Viewers can't do this",
        "Refused (401): unidentified visitor from 127.0.0.1: GET /api/events: "
        "Sign in through Cloudflare Access (no Cf-Access-Authenticated-User-Email header).",
    ]


def test_settings_lists_the_access_log(env):
    assert env.client.get("/api/logs/access").status_code == 200


def test_health_checks_arent_logged(env, log, monkeypatch, caplog):
    for addr in ("10.0.0.5", "127.0.0.1"):  # Uptime Kuma on the LAN, Docker's own check
        assert env.client.get("/api/health", environ_base={"REMOTE_ADDR": addr}).status_code == 200
    assert log() == [] and lr_auth.describe()["direct"] is None
    import web
    record = logging.LogRecord("werkzeug", logging.INFO, "", 0, '10.0.0.5 - - "GET /api/health HTTP/1.1" 200 -', None, None)
    assert not web._not_health_check(record)
    record.msg = '10.0.0.5 - - "GET /api/events HTTP/1.1" 200 -'
    assert web._not_health_check(record)


class FakeRelay:
    def __init__(self, client):
        pass

    async def close(self):
        pass


class FakeRobot:
    name = "Marvin"

    async def get_camera_status(self):
        return "ONLINE"

    def get_camera_client(self):
        class Client:
            async def generate_session(self):
                return type("Session", (), {"turn_servers": []})()
        return Client()

    async def get_camera_view(self):
        return "front"


@pytest.fixture
def live(env, log, monkeypatch):
    import lr_live

    async def robot(serial):
        return FakeRobot()
    monkeypatch.setattr(lr_live, "_robot", robot)
    monkeypatch.setattr(lr_live, "CameraSignalingRelay", FakeRelay)
    monkeypatch.setattr(lr_live, "_sessions", {})
    env.db.execute("UPDATE robots SET camera_device_id = 'cam-1'")
    env.db.commit()
    return lr_live


def start_view(env, who=ADMIN):
    r = env.client.post("/api/live/LR5-TEST-1", headers={**headers(who), **CF})
    assert r.status_code == 200
    return r.get_json()["id"]


def test_live_view_start_and_end_are_logged_with_reason(env, log, live):
    sid = start_view(env)
    env.client.post(f"/api/live/session/{sid}/close?reason=limit", headers=headers(ADMIN))
    lines = [line for line in log() if line.startswith("Live view")]
    assert lines[0] == "Live view started: alexis@example.com (Admin) from 203.0.113.7 [AU] on Marvin"
    assert lines[1].startswith("Live view ended: alexis@example.com (Admin) from 203.0.113.7 [AU] on Marvin after ")
    assert lines[1].endswith("(reached the 10-minute limit)")


def test_unknown_reasons_arent_written_to_the_log(env, log, live):
    sid = start_view(env)
    env.client.post(f"/api/live/session/{sid}/close?reason=%0AFake%20log%20line", headers=headers(ADMIN))
    assert log()[-1].endswith("(closed)")


def test_abandoned_views_are_logged_when_reaped(env, log, live):
    start_view(env)
    sid = next(iter(live._sessions))
    live.run(live.close(sid, "the page stopped responding"))
    assert log()[-1].endswith("(the page stopped responding)")


def test_viewers_cant_start_a_live_view(env, log, live):
    assert env.client.post("/api/live/LR5-TEST-1", headers=headers(VIEWER)).status_code == 403
    assert not any(line.startswith("Live view started") for line in log())
