"""SQLite store shared by the collector (lr_clips.py) and the web UI (web.py).

All timestamps are stored as UTC epoch seconds (REAL). Weights are pounds.
Cat corrections live in visit_overrides and never touch the API data, so a
re-sync can't clobber them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(os.environ.get("DB_PATH", "/data/litterrobot.db"))
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
               "image/heic": ".heic", "image/heif": ".heif"}


def photo_dir(db_path: Path | str | None = None) -> Path:
    """Cat profile photos live next to the database."""
    return Path(db_path or DB_PATH).parent / "pet-photos"


# The collector and the web UI each write their own file.
LOG_DIR = Path(os.environ.get("LOG_DIR", DB_PATH.parent / "logs"))
LOG_FILES = {"sync": "sync.log", "web": "web.log"}
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def log_file_handler(source: str) -> logging.Handler:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / LOG_FILES[source], maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


# When the web UI runs inside the collector's process, the collector registers a
# callback here so a queued request wakes it at once instead of waiting for its
# next look at the database. Call wake() only after committing the request.
_waker = None


def set_waker(fn) -> None:
    global _waker
    _waker = fn


def wake() -> None:
    if _waker:
        _waker()

# Sentinel stored in visit_overrides.pet_id for "not one of my cats / can't tell".
UNKNOWN_PET = "__unknown__"

SCHEMA = """
CREATE TABLE IF NOT EXISTS robots (
    serial           TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    folder           TEXT NOT NULL,
    timezone         TEXT,
    model            TEXT,
    camera_device_id TEXT,
    raw              TEXT,
    updated_at       REAL,
    info             TEXT,
    info_at          REAL
);
CREATE TABLE IF NOT EXISTS pets (
    pet_id              TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    pet_type            TEXT,
    gender              TEXT,
    weight              REAL,
    last_weight_reading REAL,
    is_active           INTEGER,
    image_url           TEXT,
    photo_file          TEXT,
    photo_key           TEXT,
    created_at          TEXT,
    raw                 TEXT,
    updated_at          REAL
);
CREATE TABLE IF NOT EXISTS pet_weights (
    pet_id TEXT NOT NULL,
    ts     REAL NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (pet_id, ts)
);
CREATE TABLE IF NOT EXISTS activities (
    id               TEXT PRIMARY KEY,
    robot_serial     TEXT NOT NULL,
    type             TEXT NOT NULL,
    subtype          TEXT,
    ts               REAL NOT NULL,
    pet_weight       REAL,
    waste_type       TEXT,
    waste_weight     REAL,
    duration         REAL,
    pet_ids          TEXT,
    original_pet_ids TEXT,
    camera_event_ids TEXT,
    raw              TEXT
);
CREATE TABLE IF NOT EXISTS whisker_pushes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id  TEXT NOT NULL,
    to_pet       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS whisker_pushes_activity ON whisker_pushes (activity_id, id);
CREATE TABLE IF NOT EXISTS camera_detections (
    id             TEXT PRIMARY KEY,
    robot_serial   TEXT NOT NULL,
    ts             REAL NOT NULL,
    pet_id         TEXT,
    distance       REAL,
    detection_type TEXT
);
CREATE INDEX IF NOT EXISTS camera_detections_ts ON camera_detections (robot_serial, ts);
CREATE TABLE IF NOT EXISTS firmware_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_serial TEXT NOT NULL,
    component    TEXT NOT NULL,
    version      TEXT NOT NULL,
    previous     TEXT,
    seen_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS firmware_history_robot ON firmware_history (robot_serial, component, id);
CREATE TABLE IF NOT EXISTS robot_commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_serial TEXT NOT NULL,
    command      TEXT NOT NULL,
    arg          TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activities_ts ON activities (ts);
CREATE INDEX IF NOT EXISTS activities_type_ts ON activities (type, ts);
CREATE TABLE IF NOT EXISTS clips (
    id             TEXT PRIMARY KEY,
    robot_serial   TEXT,
    robot_folder   TEXT NOT NULL,
    ts             REAL NOT NULL,
    event_type     TEXT,
    duration_s     REAL,
    pet_detections TEXT,
    video_file     TEXT,
    thumb_file     TEXT,
    raw            TEXT,
    keep           INTEGER NOT NULL DEFAULT 0,
    deleted_at     REAL
);
CREATE INDEX IF NOT EXISTS clips_ts ON clips (ts);
CREATE TABLE IF NOT EXISTS visit_overrides (
    activity_id TEXT PRIMARY KEY,
    pet_id      TEXT,
    ignored     INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS robot_snapshots (
    robot_serial       TEXT NOT NULL,
    ts                 REAL NOT NULL,
    litter_level       REAL,
    waste_drawer_level REAL,
    scale_weight       REAL,
    cycle_count        INTEGER,
    status             TEXT,
    hopper_status      TEXT,
    PRIMARY KEY (robot_serial, ts)
);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
    if "keep" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN keep INTEGER NOT NULL DEFAULT 0")
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN deleted_at REAL")
    # "ignore this visit" was removed; un-ignore anything still flagged so it counts again
    conn.execute("UPDATE visit_overrides SET ignored = 0 WHERE ignored != 0")
    conn.execute("DELETE FROM visit_overrides WHERE pet_id IS NULL AND COALESCE(note, '') = ''")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(robot_commands)")}
    if "arg" not in cols:
        conn.execute("ALTER TABLE robot_commands ADD COLUMN arg TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pets)")}
    if "photo_file" not in cols:
        conn.execute("ALTER TABLE pets ADD COLUMN photo_file TEXT")
        conn.execute("ALTER TABLE pets ADD COLUMN photo_key TEXT")
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        # photos were saved truncated (only the first network chunk); forget them so they're fetched again
        conn.execute("UPDATE pets SET photo_key = NULL, photo_file = NULL")
        conn.execute("PRAGMA user_version = 1")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(robot_snapshots)")}
    if "hopper_status" not in cols:  # Robot IQ learns the hopper's capacity from these
        conn.execute("ALTER TABLE robot_snapshots ADD COLUMN hopper_status TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(robots)")}
    if "info" not in cols:
        conn.execute("ALTER TABLE robots ADD COLUMN info TEXT")
        conn.execute("ALTER TABLE robots ADD COLUMN info_at REAL")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
    if "original_pet_ids" not in cols:
        conn.execute("ALTER TABLE activities ADD COLUMN original_pet_ids TEXT")
        conn.execute("UPDATE activities SET original_pet_ids = pet_ids")
        # cameraEvents was previously misread as cameraEventIds
        for r in conn.execute("SELECT id, raw FROM activities").fetchall():
            try:
                ids = camera_event_ids(json.loads(r["raw"] or "{}"))
            except ValueError:
                continue
            conn.execute("UPDATE activities SET camera_event_ids = ? WHERE id = ?", (json.dumps(ids), r["id"]))
    conn.commit()


def camera_event_ids(item: dict) -> list[str]:
    """IDs from cameraEvents, which may hold plain IDs or objects."""
    out = []
    for e in (item.get("cameraEvents") or item.get("cameraEventIds") or []):
        if isinstance(e, dict):
            out += [str(e[k]) for k in ("id", "eventId", "videoId", "clipId") if e.get(k)]
        elif e:
            out.append(str(e))
    return out


# ------------------------------------------------------------------ parsing


def to_epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000 if v > 1e12 else v
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    s = str(value).strip()
    if s.replace(".", "", 1).isdigit():
        return to_epoch(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_epoch(dt)


def visit_weight_lbs(raw: Any) -> float | None:
    """The activities API reports pet weight as pounds * 100 (e.g. 937 = 9.37 lb)."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return round(v / 100, 2) if v > 60 else v


def hls_duration_s(text: Any) -> float | None:
    """'01:05' -> 65.0"""
    if not text:
        return None
    try:
        secs = 0.0
        for part in str(text).split(":"):
            secs = secs * 60 + float(part)
        return secs
    except ValueError:
        return None


def activity_id(item: dict) -> str:
    if item.get("messageId"):
        return str(item["messageId"])
    key = "|".join(str(item.get(k, "")) for k in ("type", "timestamp", "petWeight", "subtype"))
    return "h-" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ writes


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def upsert_robot(conn, *, serial, name, folder, timezone_name, model, camera_device_id, raw) -> None:
    conn.execute(
        """INSERT INTO robots (serial, name, folder, timezone, model, camera_device_id, raw, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(serial) DO UPDATE SET name=excluded.name, folder=excluded.folder,
             timezone=excluded.timezone, model=excluded.model,
             camera_device_id=excluded.camera_device_id, raw=excluded.raw,
             updated_at=excluded.updated_at""",
        (serial, name, folder, timezone_name, model, camera_device_id,
         json.dumps(raw, default=str), time.time()),
    )


def upsert_pet(conn, data: dict) -> None:
    image = (data.get("profileImage") or {}).get("s3ImageURL")
    conn.execute(
        """INSERT INTO pets (pet_id, name, pet_type, gender, weight, last_weight_reading,
                             is_active, image_url, created_at, raw, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(pet_id) DO UPDATE SET name=excluded.name, pet_type=excluded.pet_type,
             gender=excluded.gender, weight=excluded.weight,
             last_weight_reading=excluded.last_weight_reading, is_active=excluded.is_active,
             image_url=excluded.image_url, created_at=excluded.created_at,
             raw=excluded.raw, updated_at=excluded.updated_at""",
        (data.get("petId"), data.get("name") or "Unnamed", data.get("type"), data.get("gender"),
         _num(data.get("weight")), _num(data.get("lastWeightReading")),
         1 if data.get("isActive", True) else 0, image, data.get("createdAt"),
         json.dumps(data, default=str), time.time()),
    )
    insert_pet_weights(conn, data.get("petId"), data.get("weightHistory") or [])


def insert_pet_weights(conn, pet_id: str, history: Iterable[dict]) -> None:
    rows = []
    for h in history:
        ts, w = to_epoch(h.get("timestamp")), _num(h.get("weight"))
        if ts is not None and w:
            rows.append((pet_id, ts, w))
    conn.executemany("INSERT OR IGNORE INTO pet_weights (pet_id, ts, weight) VALUES (?, ?, ?)", rows)


def upsert_activities(conn, robot_serial: str, items: Iterable[dict]) -> tuple[int, list[str]]:
    """Insert/refresh activities. Returns (newly inserted count, ids seen)."""
    new, ids = 0, []
    for item in items:
        ts = to_epoch(item.get("timestamp"))
        if ts is None:
            continue
        aid = activity_id(item)
        ids.append(aid)
        if not conn.execute("SELECT 1 FROM activities WHERE id = ?", (aid,)).fetchone():
            new += 1
        pet_ids = json.dumps(item.get("petIds") or [])
        # original_pet_ids keeps the robot's first guess; pet_ids tracks Whisker's
        # current assignment, which changes when a visit is reassigned
        conn.execute(
            """INSERT INTO activities (id, robot_serial, type, subtype, ts, pet_weight, waste_type,
                                       waste_weight, duration, pet_ids, original_pet_ids,
                                       camera_event_ids, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET pet_weight=excluded.pet_weight,
                 waste_type=excluded.waste_type, waste_weight=excluded.waste_weight,
                 duration=excluded.duration, pet_ids=excluded.pet_ids,
                 camera_event_ids=excluded.camera_event_ids, raw=excluded.raw""",
            (aid, robot_serial, item.get("type") or "UNKNOWN", item.get("subtype"), ts,
             visit_weight_lbs(item.get("petWeight")), item.get("wasteType"),
             _num(item.get("wasteWeight")), _num(item.get("duration")),
             pet_ids, pet_ids, json.dumps(camera_event_ids(item)),
             json.dumps(item, default=str)),
        )
    return new, ids


# ------------------------------------------------------------ Whisker write-back


def writeback_enabled(conn) -> bool:
    return get_state(conn, "writeback") != "0"  # on unless switched off in Settings


CLIP_TZ_CHOICES = ("robot", "server", "utc")


def clip_list_tz(conn) -> str:
    """Which timezone clip listings are sent with: each robot's own (default),
    the server's TZ, or UTC."""
    value = get_state(conn, "clip_tz")
    return value if value in CLIP_TZ_CHOICES else "robot"


# Whisker rejects reassigning visits older than this
WHISKER_EDIT_WINDOW_S = 7 * 86400


def whisker_editable(ts: float) -> bool:
    return ts >= time.time() - WHISKER_EDIT_WINDOW_S


def first_pet(pet_ids_json: str | None) -> str | None:
    ids = json.loads(pet_ids_json or "[]")
    return ids[0] if ids else None


def enqueue_push(conn, activity_id: str, to_pet: str | None) -> None:
    """Ask the collector to set this visit's cat in Whisker (None = no cat).
    Only the newest request per visit matters, so older unsent ones are dropped."""
    conn.execute("DELETE FROM whisker_pushes WHERE activity_id = ? AND status = 'pending'", (activity_id,))
    now = time.time()
    conn.execute("INSERT INTO whisker_pushes (activity_id, to_pet, status, created_at, updated_at) "
                 "VALUES (?, ?, 'pending', ?, ?)", (activity_id, to_pet, now, now))


def latest_push(conn, activity_id: str):
    return conn.execute("SELECT * FROM whisker_pushes WHERE activity_id = ? ORDER BY id DESC LIMIT 1",
                        (activity_id,)).fetchone()


def has_pending_pushes(conn) -> bool:
    return conn.execute("SELECT 1 FROM whisker_pushes WHERE status = 'pending' LIMIT 1").fetchone() is not None


def finish_push(conn, push_id: int, activity_id: str, to_pet: str | None, error: str | None) -> None:
    now = time.time()
    conn.execute("UPDATE whisker_pushes SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                 ("error" if error else "done", error, now, push_id))
    if not error:  # reflect the new assignment now rather than waiting for the next sync
        conn.execute("UPDATE activities SET pet_ids = ? WHERE id = ?",
                     (json.dumps([to_pet] if to_pet else []), activity_id))
    conn.commit()


# ------------------------------------------------------------ robot commands

# web-facing name -> pylitterbot method (None = just refresh the status)
ROBOT_COMMANDS = {
    "clean": "start_cleaning",
    "reset_drawer": "reset_waste_drawer",
    "remote_reset": "reset",
    "refresh": None,
}


def set_robot_info(conn, serial: str, info: dict) -> None:
    conn.execute("UPDATE robots SET info = ?, info_at = ? WHERE serial = ?",
                 (json.dumps(info, default=str), time.time(), serial))
    record_firmware(conn, serial, info.get("firmware"))


def upsert_detections(conn, serial: str, events: Iterable[dict]) -> int:
    """Store the camera's own cat recognition from its ai_event feed.
    association.distance is a cosine distance: lower means a closer match."""
    new = 0
    for e in events:
        if e.get("type") != "ai_event":
            continue
        data = ((e.get("metadata") or {}).get("payload") or {}).get("data") or {}
        assoc = (data.get("object") or {}).get("association") or {}
        ts = to_epoch(e.get("createdAt"))
        if ts is None or not e.get("id"):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO camera_detections (id, robot_serial, ts, pet_id, distance, detection_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(e["id"]), serial, ts, assoc.get("id"), _num(assoc.get("distance")), data.get("detection_type")))
        new += cur.rowcount
    return new


def parse_firmware(text: str | None) -> dict[str, str]:
    """'ESP: 1.4.2 / MCU: 2.1.0 / CAM: 1.2.2' -> {'ESP': '1.4.2', 'MCU': '2.1.0', 'CAM': '1.2.2'}"""
    out = {}
    for part in (text or "").split("/"):
        name, sep, version = part.partition(":")
        if sep and name.strip() and version.strip():
            out[name.strip()] = version.strip()
    return out


def record_firmware(conn, serial: str, firmware: str | None) -> None:
    """Log each firmware component whose version differs from the last one recorded.
    The first sighting of a component has previous = NULL (its install date is unknown)."""
    for component, version in parse_firmware(firmware).items():
        last = conn.execute("SELECT version FROM firmware_history WHERE robot_serial = ? AND component = ? "
                            "ORDER BY id DESC LIMIT 1", (serial, component)).fetchone()
        if last and last["version"] == version:
            continue
        conn.execute("INSERT INTO firmware_history (robot_serial, component, version, previous, seen_at) "
                     "VALUES (?, ?, ?, ?, ?)", (serial, component, version, last["version"] if last else None, time.time()))


def enqueue_command(conn, serial: str, command: str, arg: str | None = None) -> int:
    now = time.time()
    cur = conn.execute("INSERT INTO robot_commands (robot_serial, command, arg, status, created_at, updated_at) "
                       "VALUES (?, ?, ?, 'pending', ?, ?)", (serial, command, arg, now, now))
    return cur.lastrowid


def active_command(conn, serial: str, command: str, arg: str | None = None):
    return conn.execute("SELECT * FROM robot_commands WHERE robot_serial = ? AND command = ? AND arg IS ? "
                        "AND status IN ('pending', 'sending') ORDER BY id DESC LIMIT 1",
                        (serial, command, arg)).fetchone()


def clip_download_status(conn, clip_ids) -> dict:
    """Latest (re-)download request per clip id."""
    ids = [str(i) for i in clip_ids]
    if not ids:
        return {}
    out = {}
    for r in conn.execute(f"SELECT arg, status, error, updated_at FROM robot_commands WHERE command = 'redownload' "
                          f"AND arg IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
        out[r["arg"]] = {"status": r["status"], "error": r["error"], "updated": r["updated_at"]}
    return out


def has_pending_commands(conn) -> bool:
    return conn.execute("SELECT 1 FROM robot_commands WHERE status = 'pending' LIMIT 1").fetchone() is not None


def upsert_clip(conn, *, clip_id, robot_serial, robot_folder, raw, video_file=None, thumb_file=None) -> None:
    ts = to_epoch(raw.get("createdAt"))
    if ts is None:
        return
    conn.execute(
        """INSERT INTO clips (id, robot_serial, robot_folder, ts, event_type, duration_s,
                              pet_detections, video_file, thumb_file, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             robot_serial=COALESCE(excluded.robot_serial, clips.robot_serial),
             robot_folder=excluded.robot_folder, ts=excluded.ts, event_type=excluded.event_type,
             duration_s=excluded.duration_s, pet_detections=excluded.pet_detections,
             video_file=COALESCE(excluded.video_file, clips.video_file),
             thumb_file=COALESCE(excluded.thumb_file, clips.thumb_file), raw=excluded.raw""",
        (str(clip_id), robot_serial, robot_folder, ts, raw.get("eventType"),
         hls_duration_s(raw.get("hlsDuration")), json.dumps(raw.get("petDetections") or []),
         video_file, thumb_file, json.dumps(raw, default=str)),
    )


def add_snapshot(conn, *, robot_serial, litter_level, waste_drawer_level, scale_weight, cycle_count, status,
                 hopper_status=None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO robot_snapshots (robot_serial, ts, litter_level, waste_drawer_level, scale_weight, "
        "cycle_count, status, hopper_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (robot_serial, time.time(), litter_level, waste_drawer_level, scale_weight, cycle_count, status,
         hopper_status),
    )


def is_clip_deleted(conn, clip_id) -> bool:
    row = conn.execute("SELECT deleted_at FROM clips WHERE id = ?", (str(clip_id),)).fetchone()
    return bool(row and row["deleted_at"])


def apply_retention(conn, clips_root: Path, days: int, log=None) -> int:
    """Delete clip files (mp4 + jpg + json) older than `days` unless marked keep."""
    cutoff = time.time() - days * 86400
    root = clips_root.resolve()
    rows = conn.execute(
        "SELECT id, robot_folder, video_file FROM clips WHERE keep = 0 AND deleted_at IS NULL "
        "AND video_file IS NOT NULL AND ts < ?", (cutoff,)
    ).fetchall()
    deleted = 0
    for r in rows:
        stem = Path(r["video_file"]).stem
        folder = (root / r["robot_folder"]).resolve()
        if not folder.is_relative_to(root):
            continue
        try:
            for ext in (".mp4", ".jpg", ".json"):
                (folder / f"{stem}{ext}").unlink(missing_ok=True)
        except OSError as err:
            if log:
                log.warning("retention: could not delete %s: %s", r["video_file"], err)
            continue
        conn.execute("UPDATE clips SET video_file = NULL, thumb_file = NULL, deleted_at = ? WHERE id = ?",
                     (time.time(), r["id"]))
        deleted += 1
    conn.commit()
    return deleted


def index_clip_files(conn, clips_root: Path) -> int:
    """Index already-downloaded clips from their JSON sidecars (covers clips that
    have aged out of the API but are still on disk)."""
    if not clips_root.is_dir():
        return 0
    folders = {r["folder"]: r["serial"] for r in conn.execute("SELECT folder, serial FROM robots")}
    known = {r["video_file"] for r in conn.execute("SELECT video_file FROM clips WHERE video_file IS NOT NULL")}
    added = 0
    for folder in clips_root.iterdir():
        if not folder.is_dir():
            continue
        for mp4 in folder.glob("*.mp4"):
            if mp4.name.endswith(".part.mp4") or mp4.name in known:
                continue
            sidecar = mp4.with_suffix(".json")
            if not sidecar.exists():
                continue
            try:
                raw = json.loads(sidecar.read_text())
            except (OSError, ValueError):
                continue
            thumb = mp4.with_suffix(".jpg")
            upsert_clip(conn, clip_id=raw.get("id", mp4.stem), robot_serial=folders.get(folder.name),
                        robot_folder=folder.name, raw=raw, video_file=mp4.name,
                        thumb_file=thumb.name if thumb.exists() else None)
            added += 1
    return added
