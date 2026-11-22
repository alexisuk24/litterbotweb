"""Settings API (sync schedule, retention, Whisker update counts) and the health check."""
import asyncio
import sqlite3
import time

import pytest

import lr_clips
import lr_db
import web

DAY = 86400


def test_sync_interval_setting(env):
    assert env.client.get("/api/settings").get_json()["sync_interval"] == 3600
    assert env.client.post("/api/settings", json={"sync_interval": 60}).status_code == 400
    assert env.client.post("/api/settings", json={"sync_interval": 86400}).get_json()["sync_interval"] == 86400


def test_collector_picks_up_a_shorter_interval_while_waiting(env, monkeypatch):
    monkeypatch.setattr(lr_db, "SYNC_INTERVALS", lr_db.SYNC_INTERVALS + (1,))
    env.state(sync_interval=86400, refresh_requested=0)

    async def run():
        wake = asyncio.Event()
        task = asyncio.create_task(lr_clips.wait_for_next_run(env.db, 3600, wake))
        await asyncio.sleep(0.2)
        env.state(sync_interval=1)
        wake.set()
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(run()) == "scheduled"


def test_whisker_update_counts_skip_visits_too_old_to_change(env):
    env.robot()
    old, new = env.visit(30 * DAY), env.visit(3600)
    now = time.time()
    for aid in (old, new):
        env.db.execute("INSERT INTO whisker_pushes (activity_id, status, created_at, updated_at) "
                       "VALUES (?, 'error', ?, ?)", (aid, now, now))
    env.db.execute("INSERT INTO whisker_pushes (activity_id, status, created_at, updated_at) "
                   "VALUES (?, 'pending', ?, ?)", (old, now, now))
    env.db.commit()
    st = env.client.get("/api/settings").get_json()
    assert (st["failed"], st["pending"]) == (1, 0)


@pytest.mark.parametrize("state, status", [
    ({}, 200),                                                                     # fresh start
    ({"sync_started": -1000, "sync_finished": -900, "sync_running": 0}, 200),      # healthy
    ({"sync_started": -4 * 3600, "sync_running": 1}, 503),                         # stuck sync
    ({"sync_started": -5 * 3600, "sync_finished": -5 * 3600, "sync_running": 0}, 503),  # no sync for 5 h
    ({"sync_started": -100, "sync_finished": -50, "sync_running": 0, "sync_error": "Whisker 503"}, 200),
])
def test_health(env, state, status):
    now = time.time()
    env.state(sync_interval=3600, **{k: (now + v if k in ("sync_started", "sync_finished") else v)
                                     for k, v in state.items()})
    assert env.client.get("/api/health").status_code == status


def test_health_keeps_database_errors_in_the_log(env, monkeypatch, caplog):
    def broken():
        raise sqlite3.OperationalError("unable to open database file /data/litterrobot.db")
    monkeypatch.setattr(web, "db", broken)
    resp = env.client.get("/api/health")
    assert resp.status_code == 503
    assert resp.get_json()["problem"].startswith("database:")
    assert "/data/litterrobot.db" not in resp.get_data(as_text=True)
    assert "/data/litterrobot.db" in caplog.text


def test_health_follows_a_daily_schedule(env):
    now = time.time()
    env.state(sync_interval=86400, sync_started=now - 30 * 3600, sync_finished=now - 30 * 3600, sync_running=0)
    assert env.client.get("/api/health").status_code == 200


def test_pages_get_the_weight_unit_and_user(env):
    env.robot()
    env.client.post("/api/settings", json={"unit": "lb"})
    for page in ("/events", "/cats", "/clips", "/settings", "/robots/LR5-TEST-1"):
        html = env.client.get(page).data.decode()
        assert 'window.LR_UNIT = "lb"' in html and "window.LR_USER" in html, page


def test_clips_page_defines_the_helpers_it_uses(env):
    """It doesn't load common.js, so anything it calls from there must be defined on the page."""
    html = env.client.get("/clips").data.decode()
    for helper in ("retentionNote", "expiryText", "esc"):
        assert f"function {helper}(" in html or f"const {helper} =" in html, helper
