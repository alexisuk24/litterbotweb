"""Clip retention: full quality, then a small copy, then only a picture; Keep forever is untouched."""
import argparse
import asyncio
import hashlib
import shutil
import subprocess

import pytest

import lr_clips
import lr_db
import web

DAY = 86400
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def sample_mp4(tmp_path_factory):
    """Two seconds of 1080p test pattern with sound."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "18", "-c:a", "aac", "-shortest", str(out)], check=True)
    return out.read_bytes()


def run_retention(env):
    env.state(retention_enabled_at=env.now - 30 * DAY)  # past the grace period
    asyncio.run(lr_clips.apply_retention(env.db, argparse.Namespace(retention_grace_hours=24, out=str(env.clips))))


def row(env, cid):
    return env.db.execute("SELECT * FROM clips WHERE id = ?", (cid,)).fetchone()


@needs_ffmpeg
def test_three_steps_and_keep_forever(env, sample_mp4):
    env.state(retention_default=14, retention_min_days=9, retention_small="30")
    shrink = env.clip(20 * DAY, video_bytes=sample_mp4)
    drop = env.clip(40 * DAY, video_bytes=sample_mp4)
    no_thumb = env.clip(40 * DAY, video_bytes=sample_mp4, thumb=False)
    kept = env.clip(40 * DAY, video_bytes=sample_mp4, keep=True)
    fresh = env.clip(2 * DAY, video_bytes=sample_mp4)
    run_retention(env)

    small = env.clips / "Marvin" / row(env, shrink)["video_file"]
    assert row(env, shrink)["quality"] == "small" and small.stat().st_size < len(sample_mp4)
    for cid in (drop, no_thumb):
        r = row(env, cid)
        assert r["video_file"] is None and r["deleted_at"] and (env.clips / "Marvin" / r["thumb_file"]).exists()
    kept_file = env.clips / "Marvin" / row(env, kept)["video_file"]
    assert hashlib.sha1(kept_file.read_bytes()).digest() == hashlib.sha1(sample_mp4).digest()
    assert row(env, fresh)["quality"] is None


def test_without_a_small_copy_old_clips_keep_only_their_picture(env):
    env.state(retention_default=14, retention_min_days=9)
    old = env.clip(20 * DAY)
    run_retention(env)
    r = row(env, old)
    assert r["video_file"] is None and r["thumb_file"]


def test_forever_changes_nothing(env):
    env.state(retention_default=14, retention_min_days=9)
    env.client.post("/api/settings", json={"retention_days": 0})
    old = env.clip(400 * DAY)
    run_retention(env)
    assert row(env, old)["video_file"]


def test_settings_validation_and_grace_period(env):
    env.state(retention_default=14, retention_min_days=9, retention_enabled_at=1)
    c = env.client
    assert c.post("/api/settings", json={"retention_days": 7}).status_code == 400   # inside the download window
    assert c.post("/api/settings", json={"retention_small": 7}).status_code == 400

    def restarted(body):
        env.state(retention_enabled_at=1)
        c.post("/api/settings", json=body)
        return float(lr_db.get_state(lr_db.connect(), "retention_enabled_at")) > 1

    # nothing happens sooner: shrunk at 14 days instead of losing the video then
    assert not restarted({"retention_small": 30})
    assert not restarted({"retention_small": 60})
    assert restarted({"retention_small": "none"})      # the video goes at 14 days again, not 60
    assert not restarted({"retention_days": 30})
    assert restarted({"retention_days": 14})


def test_clip_notes_say_whats_next(env):
    env.state(retention_default=14, retention_min_days=9, retention_small="60")
    cid = env.clip(3 * DAY)
    with web.app.test_request_context():
        f = web.keep_fields(row(env, cid))
    assert f["next"] == "small" and round((f["expires"] - env.now) / DAY) == 11
