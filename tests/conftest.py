"""Shared fixtures: every test gets its own empty database and clips folder, with access
control off, plus helpers to add robots, cats, visits and clips."""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lr_auth  # noqa: E402
import lr_db  # noqa: E402
import web  # noqa: E402

DAY = 86400
_ids = itertools.count(1)


class Env:
    def __init__(self, conn, root: Path):
        self.db, self.root, self.now = conn, root, time.time()
        self.clips = root / "clips"
        self.client = web.app.test_client()

    def robot(self, serial="LR5-TEST-1", name="Marvin", info=None, raw=None, camera=True):
        self.db.execute("INSERT INTO robots (serial, name, folder, timezone, model, camera_device_id, raw, info, info_at) "
                        "VALUES (?, ?, ?, 'UTC', 'Litter-Robot 5 Pro', ?, ?, ?, ?)",
                        (serial, name, name, "cam-1" if camera else None, json.dumps(raw or {}),
                         json.dumps(info) if info is not None else None, self.now))
        self.db.commit()
        return serial

    def pet(self, pid, name, weight_lb=9.0, active=True):
        self.db.execute("INSERT INTO pets (pet_id, name, weight, is_active, created_at) VALUES (?, ?, ?, ?, ?)",
                        (pid, name, weight_lb, int(active), f"2026-01-{next(_ids) % 28 + 1:02d}"))
        self.db.commit()
        return pid

    def visit(self, ago_s, pet=None, weight=9.0, duration=60, waste="Urine", robot="LR5-TEST-1", kind="PET_VISIT"):
        aid = f"act-{next(_ids)}"
        pets = json.dumps([pet] if pet else [])
        self.db.execute("INSERT INTO activities (id, robot_serial, type, ts, pet_weight, waste_type, duration, pet_ids, "
                        "original_pet_ids, camera_event_ids, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)",
                        (aid, robot, kind, self.now - ago_s, weight if kind == "PET_VISIT" else None,
                         waste if kind == "PET_VISIT" else None, duration if kind == "PET_VISIT" else None,
                         pets, pets, json.dumps({"eventId": f"evt-{aid}"})))
        self.db.commit()
        return aid

    def clip(self, ago_s, robot_folder="Marvin", video_bytes: bytes | None = b"x", thumb=True, keep=False):
        """A clip row; with video_bytes, also a file (a real MP4 when given one)."""
        cid = f"clip-{next(_ids)}"
        stem = f"{robot_folder}_{cid}"
        folder = self.clips / robot_folder
        folder.mkdir(parents=True, exist_ok=True)
        video = None
        if video_bytes is not None:
            (folder / f"{stem}.mp4").write_bytes(video_bytes)
            video = f"{stem}.mp4"
        if thumb:
            (folder / f"{stem}.jpg").write_bytes(b"\xff\xd8jpeg")
        self.db.execute("INSERT INTO clips (id, robot_serial, robot_folder, ts, video_file, thumb_file, raw, keep) "
                        "VALUES (?, 'LR5-TEST-1', ?, ?, ?, ?, '{}', ?)",
                        (cid, robot_folder, self.now - ago_s, video, f"{stem}.jpg" if thumb else None, int(keep)))
        self.db.commit()
        return cid

    def state(self, **kv):
        for k, v in kv.items():
            lr_db.set_state(self.db, k, str(v))
        self.db.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(lr_db, "DB_PATH", tmp_path / "litterrobot.db")
    monkeypatch.setattr(web, "CLIPS", tmp_path / "clips")
    for name, value in (("HEADER", ""), ("TEAM", ""), ("AUD", ""), ("ADMINS", set()), ("_direct", None),
                        ("_jwks", None), ("_sessions", {}), ("_refusals", {})):
        monkeypatch.setattr(lr_auth, name, value)
    monkeypatch.setattr(lr_db, "_waker", None)
    (tmp_path / "clips").mkdir()
    conn = lr_db.connect()
    yield Env(conn, tmp_path)
    conn.close()
