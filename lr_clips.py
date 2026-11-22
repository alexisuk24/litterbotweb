#!/usr/bin/env python3
"""Download recorded Litter-Robot 5 Pro camera clips from the Whisker cloud.

Uses pylitterbot (>= 2025.9.0) for login and the camera inventory API, then
downloads each clip's MP4 chunks from their presigned S3 URLs and joins them
into one MP4 with ffmpeg (re-encoding clips whose chunks change resolution,
with VAAPI when /dev/dri is mapped in).

Each run also syncs metadata into a SQLite database (--db / DB_PATH): robots,
pets and their weight history, the robot activity log (pet visits with weight,
duration and waste type), robot state snapshots, and an index of every clip.

Credentials come from the environment: LR_USERNAME, LR_PASSWORD.

Files are saved as: {out}/{RobotName}/{RobotName}_{YYYY-MM-DD}_{HH-MM-SS}.mp4

  python lr_clips.py --out ./clips                # last 7 days, both robots
  python lr_clips.py --out ./clips --days 2
  python lr_clips.py --dump ./api-dump            # save raw API JSON, no video
  python lr_clips.py --out ./clips --interval 3600   # keep running, poll hourly
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from yarl import URL
from pylitterbot import Account
from pylitterbot.camera import VideoClip, VideoPlayback, local_timezone_name
from pylitterbot.robot.litterrobot5 import LitterRobot5

import lr_db
import lr_notify

LOG = logging.getLogger("lr_clips")
ACTIVITY_PAGE = 100
SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(text: str) -> str:
    return SAFE.sub("_", text).strip("_") or "unknown"


def check_and_update_pylitterbot() -> None:
    LOG.info("Checking for pylitterbot updates…")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pylitterbot"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if "Successfully installed" in proc.stdout:
            LOG.info("pylitterbot updated: %s", last_line)
        else:
            LOG.info("pylitterbot is already up to date")
    else:
        LOG.warning("pylitterbot update check failed: %s", proc.stderr.strip()[-200:])


# ----------------------------------------------------------------------- video


async def fetch(http: aiohttp.ClientSession, url: str, cookie: str) -> bytes:
    async with http.get(url, headers={"Cookie": cookie}) as resp:
        resp.raise_for_status()
        return await resp.read()


class ClipNotReady(Exception):
    """Whisker lists the clip but hasn't stored its video chunks yet."""


HWACCEL = os.environ.get("LR_HWACCEL", "auto").lower()  # auto | vaapi | off
_vaapi_failed = False  # set once VAAPI has failed, so later clips go straight to software


def vaapi_device() -> str | None:
    """The VAAPI render node to encode on, if /dev/dri is mapped in and allowed."""
    if HWACCEL == "off" or _vaapi_failed:
        return None
    nodes = sorted(Path("/dev/dri").glob("renderD*"))
    if not nodes:
        if HWACCEL == "vaapi":
            LOG.warning("LR_HWACCEL=vaapi but no /dev/dri/renderD* device is mapped in; encoding in software")
        return None
    return str(nodes[0])


def probe_chunk(path: Path) -> tuple[tuple[int, int], bool]:
    """A chunk's video (width, height), and whether it has audio."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(path)],
        capture_output=True, text=True)
    streams = json.loads(out.stdout or "{}").get("streams", [])
    video = next((st for st in streams if st.get("codec_type") == "video"), {})
    return (video.get("width", 0), video.get("height", 0)), any(st.get("codec_type") == "audio" for st in streams)


def reencode_cmd(chunks: list[Path], size: tuple[int, int], audio: bool, device: str | None, out: Path) -> list[str]:
    """Join chunks of different resolutions into one H.264 MP4 at size."""
    w, h = size
    inputs, graph = [], []
    for i, chunk in enumerate(chunks):
        inputs += ["-i", str(chunk)]
        graph.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
        if audio:
            graph.append(f"[{i}:a]aresample=48000[a{i}]")
    pairs = "".join(f"[v{i}]" + (f"[a{i}]" if audio else "") for i in range(len(chunks)))
    graph.append(f"{pairs}concat=n={len(chunks)}:v=1:a={int(audio)}[cv]" + ("[ca]" if audio else ""))
    if device:
        graph.append("[cv]format=nv12,hwupload[v]")
        enc = ["-c:v", "h264_vaapi", "-qp", "22"]
        hw = ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"]
    else:
        graph.append("[cv]format=yuv420p[v]")
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]
        hw = []
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *hw, *inputs,
            "-filter_complex", ";".join(graph), "-map", "[v]", *(["-map", "[ca]", "-c:a", "aac", "-b:a", "128k"] if audio else []),
            *enc, "-movflags", "+faststart", str(out)]


async def run_ffmpeg(cmd: list[str], partial: Path) -> None:
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")


async def join_chunks(chunks: list[Path], dest: Path, clip_id: str) -> None:
    """Join a clip's MP4 chunks into dest. Chunks of one resolution are joined
    as-is; Whisker clips that switch cameras (front at 1080p, then globe at 720p)
    are re-encoded at the largest size, since players freeze at a resolution
    change. Writes to a temp file first, so a failure leaves any existing dest intact."""
    global _vaapi_failed
    partial = dest.with_name(f"{dest.stem}.{os.urandom(4).hex()}.part.mp4")
    probes = await asyncio.gather(*(asyncio.to_thread(probe_chunk, c) for c in chunks))
    sizes = list(dict.fromkeys(size for size, _ in probes))
    if len(sizes) == 1:
        listing = chunks[0].parent / "chunks.txt"
        listing.write_text("".join(f"file '{c.name}'\n" for c in chunks))
        await run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-i", str(listing),
                          "-c", "copy", "-movflags", "+faststart", str(partial)], partial)
    else:
        size = max(sizes, key=lambda s: s[0] * s[1])
        audio = all(has_audio for _, has_audio in probes)
        if not audio:
            LOG.warning("clip %s: some chunks have no audio; saving it without sound", clip_id)
        change = " -> ".join(f"{w}x{h}" for w, h in sizes)
        device = vaapi_device()
        if device:
            LOG.info("clip %s: resolution changes (%s); re-encoding at %dx%d with VAAPI", clip_id, change, *size)
            try:
                await run_ffmpeg(reencode_cmd(chunks, size, audio, device, partial), partial)
            except RuntimeError as err:
                _vaapi_failed = True
                LOG.warning("VAAPI encoding failed, using software for the rest of this run: %s", err)
                device = None
        if not device:
            LOG.info("clip %s: resolution changes (%s); re-encoding at %dx%d in software", clip_id, change, *size)
            await run_ffmpeg(reencode_cmd(chunks, size, audio, None, partial), partial)
    partial.rename(dest)


async def download_clip(client, http: aiohttp.ClientSession, clip_id: str, dest: Path) -> VideoPlayback | None:
    """Download a clip's MP4 chunks from their presigned S3 URLs and join them
    into dest (see join_chunks). Returns the playback details (its CloudFront
    cookies fetch the thumbnail), or None if Whisker has no such clip."""
    playback = await client.get_video_playback(clip_id)
    if playback is None:
        return None
    if not playback.chunks:
        raise ClipNotReady(clip_id)
    with tempfile.TemporaryDirectory(prefix="lrclip-") as tmp:
        chunks = []
        for i, chunk in enumerate(playback.chunks):
            # presigned: no auth or cookies, and the query string must stay exactly as signed
            async with http.get(URL(chunk.download_url, encoded=True)) as resp:
                resp.raise_for_status()
                path = Path(tmp) / f"chunk{i:03d}.mp4"
                path.write_bytes(await resp.read())
            chunks.append(path)
        await join_chunks(chunks, dest, clip_id)
    return playback


# ------------------------------------------------------------------------ clips


def date_window(days: int, tz: ZoneInfo) -> list[str]:
    """Dates to query, as days in tz (how Whisker reads them when given a
    timezone). One extra day either side for safety; clips dedupe by id."""
    today = datetime.now(tz).date()
    return [(today - timedelta(d)).isoformat() for d in range(-1, days + 1)]


def clip_list_tz_name(db, robot: LitterRobot5, fallback: str) -> str:
    """The timezone to list clips in, per the Settings page choice."""
    choice = lr_db.clip_list_tz(db)
    if choice == "server":
        return local_timezone_name()
    if choice == "utc":
        return "UTC"
    return robot.timezone or fallback


async def list_clips(robot: LitterRobot5, days: int, tz_name: str, raw_lists: dict | None = None) -> list:
    """Every clip Whisker lists for the date window, listed in tz_name (dates
    are read as days in it) and merged by id. raw_lists, if given, collects
    each listing's clips as-is."""
    seen: dict[str, VideoClip] = {}
    for day in [None, *date_window(days, ZoneInfo(tz_name))]:  # None: newest, no date filter
        label = day or "latest"
        try:
            listed = await robot.get_camera_videos(date=day, timezone=tz_name)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOG.warning("%s: Whisker's clip list for %s failed: %s", robot.name, label, err)
            continue
        if raw_lists is not None:
            raw_lists[label] = [c.raw for c in listed]
        for clip in listed:
            seen.setdefault(clip.id, clip)
    return sorted(seen.values(), key=lambda c: c.created_at or datetime.min.replace(tzinfo=timezone.utc))


CLIP_STALE_AFTER_S = 3 * 3600


def check_clip_list_freshness(db, robot: LitterRobot5, clips: list) -> None:
    """Warn when visits keep coming but Whisker's clip list stops growing: downloads
    can't fail in that case, so the stall would otherwise go unnoticed."""
    newest_clip = max((c.created_at.timestamp() for c in clips if c.created_at), default=None)
    row = db.execute("SELECT MAX(ts) AS ts, COUNT(*) AS n FROM activities WHERE robot_serial = ? "
                     "AND type = 'PET_VISIT' AND ts > ?", (robot.serial, newest_clip or 0)).fetchone()
    stale = newest_clip is not None and row["ts"] and row["ts"] - newest_clip > CLIP_STALE_AFTER_S
    key = f"clips_stale:{robot.serial}"
    if stale:
        since = datetime.fromtimestamp(newest_clip).strftime("%d %b %H:%M")
        LOG.warning("%s: Whisker's clip list hasn't updated since %s, but %d visit(s) have happened since; "
                    "their videos will download if Whisker lists them later", robot.name, since, row["n"])
        lr_db.set_state(db, key, json.dumps({"newest_clip": newest_clip, "visits_since": row["n"]}))
    else:
        lr_db.set_state(db, key, "")
    db.commit()


async def process_robot(
    robot: LitterRobot5, http: aiohttp.ClientSession, args: argparse.Namespace
) -> tuple[int, int]:
    tz = ZoneInfo(robot.timezone or args.tz)
    client = robot.get_camera_client()
    raw_lists: dict = {}
    list_tz = clip_list_tz_name(args.db_conn, robot, args.tz)
    clips = await list_clips(robot, args.days, list_tz, raw_lists)
    LOG.info("%s: %d clip(s) listed (%s)", robot.name, len(clips), list_tz)
    if not args.dump:
        check_clip_list_freshness(args.db_conn, robot, clips)

    if args.dump:
        d = Path(args.dump) / slug(robot.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "robot.json").write_text(json.dumps(robot.to_dict(), indent=2, default=str))
        (d / "clips.json").write_text(json.dumps([c.raw for c in clips], indent=2))
        (d / "clips_by_date.json").write_text(json.dumps(raw_lists, indent=2))  # each listing as-is
        (d / "events.json").write_text(json.dumps(await client.get_events(limit=200), indent=2))
        (d / "camera_info.json").write_text(json.dumps(await client.get_camera_info(), indent=2))
        (d / "activities.json").write_text(json.dumps(await robot.get_activities(limit=200), indent=2))
        (d / "pets.json").write_text(
            json.dumps([p.to_dict() for p in robot._account.pets], indent=2, default=str))
        if clips:
            pb = await client.get_video_playback(clips[-1].id)
            if pb:
                (d / "playback_sample.json").write_text(json.dumps(pb.raw, indent=2))
        LOG.info("%s: API dump written to %s", robot.name, d)
        return 0, 0

    db = args.db_conn
    got = failed = 0
    for clip in clips:
        when = (clip.created_at or datetime.now(timezone.utc)).astimezone(tz)
        folder = Path(args.out) / slug(robot.name)
        stem = f"{slug(robot.name)}_{when:%Y-%m-%d}_{when:%H-%M-%S}"
        mp4 = folder / f"{stem}.mp4"
        thumb = folder / f"{stem}.jpg"
        if mp4.exists():
            lr_db.upsert_clip(db, clip_id=clip.id, robot_serial=robot.serial, robot_folder=folder.name,
                              raw=clip.raw, video_file=mp4.name,
                              thumb_file=thumb.name if thumb.exists() else None)
            db.commit()
            continue
        if lr_db.is_clip_deleted(db, clip.id):  # removed by retention; don't fetch it again
            continue
        lr_db.upsert_clip(db, clip_id=clip.id, robot_serial=robot.serial,
                          robot_folder=folder.name, raw=clip.raw)
        db.commit()
        folder.mkdir(parents=True, exist_ok=True)

        try:
            playback = await download_clip(client, http, clip.id, mp4)  # fresh S3 URLs and cookies per clip
        except ClipNotReady:
            LOG.info("%s: clip %s isn't ready on Whisker yet; will try again next sync", robot.name, clip.id)
            continue
        except Exception as err:  # keep going; one bad clip shouldn't stop the run
            LOG.warning("%s: clip %s failed: %s", robot.name, clip.id, err)
            failed += 1
            continue
        if playback is None:
            LOG.warning("%s: no playback for clip %s", robot.name, clip.id)
            failed += 1
            continue

        (folder / f"{stem}.json").write_text(json.dumps(clip.raw, indent=2))
        if clip.thumbnail_url and not args.no_thumbs:
            try:
                thumb.write_bytes(await fetch(http, clip.thumbnail_url, playback.cookie_header))
            except Exception as err:
                LOG.debug("thumbnail failed for %s: %s", clip.id, err)
        lr_db.upsert_clip(db, clip_id=clip.id, robot_serial=robot.serial, robot_folder=folder.name,
                          raw=clip.raw, video_file=mp4.name,
                          thumb_file=thumb.name if thumb.exists() else None)
        db.commit()
        got += 1
        LOG.info("%s: saved %s", robot.name, mp4.relative_to(args.out))
    return got, failed


# --------------------------------------------------------------------- metadata


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _epoch(dt):
    return dt.timestamp() if dt else None


def robot_info(robot: LitterRobot5) -> dict:
    """Snapshot of the robot's state for the Robots page."""
    status = _safe(lambda: robot.status)
    level_state = _safe(lambda: robot.litter_level_state)
    faults = {
        "Waste drawer full": _safe(lambda: robot.is_waste_drawer_full),
        "Drawer-full indicator on": _safe(lambda: robot.is_drawer_full_indicator_triggered),
        "Waste drawer removed": _safe(lambda: robot.is_drawer_removed),
        "Bonnet removed": _safe(lambda: robot.is_bonnet_removed),
        "Laser sensor needs cleaning": _safe(lambda: robot.is_laser_dirty),
        "Gas sensor fault": _safe(lambda: robot.is_gas_sensor_fault_detected),
        "USB fault": _safe(lambda: robot.is_usb_fault_detected),
    }
    motor = _safe(lambda: robot.globe_motor_fault_status)
    motor_name = getattr(motor, "name", None)
    if motor_name and motor_name not in ("NONE", "OK", "UNKNOWN", "FAULT_CLEAR"):  # FAULT_CLEAR: no fault
        faults[f"Globe motor: {motor_name.replace('_', ' ').lower()}"] = True
    return {
        "status": getattr(status, "text", None) or (str(status) if status else None),
        "status_code": _safe(lambda: robot.status_code),
        "online": _safe(lambda: robot.is_online),
        "on": _safe(lambda: robot.is_on),
        "sleeping": _safe(lambda: robot.is_sleeping),
        "sleep_mode": _safe(lambda: robot.sleep_mode_enabled),
        "last_seen": _epoch(_safe(lambda: robot.last_seen)),
        "setup_date": _epoch(_safe(lambda: robot.setup_date)),
        "model": _safe(lambda: robot.model),
        "litter_level": _safe(lambda: robot.litter_level),
        "litter_state": getattr(level_state, "name", None),
        "hopper": _safe(lambda: robot.hopper_status_text),
        "drawer_level": _safe(lambda: robot.waste_drawer_level),
        "cycles_since_empty": _safe(lambda: robot.cycle_count),
        "total_cycles": _safe(lambda: robot.odometer_empty_cycles),
        "filter_cycles": _safe(lambda: robot.odometer_filter_cycles),
        "next_filter": _epoch(_safe(lambda: robot.next_filter_replacement_date)),
        "scoops_saved": _safe(lambda: robot.scoops_saved_count),
        "wait_minutes": _safe(lambda: robot.clean_cycle_wait_time_minutes),
        "wifi_rssi": _safe(lambda: robot.wifi_rssi),
        "firmware": _safe(lambda: robot.firmware),
        "firmware_status": _safe(lambda: robot.firmware_update_status),
        "firmware_updating": _safe(lambda: robot.is_firmware_updating),
        "faults": [name for name, on in faults.items() if on],
    }


def sync_robot(db, robot: LitterRobot5) -> None:
    status = _safe(lambda: robot.status)
    lr_db.upsert_robot(
        db, serial=robot.serial, name=robot.name, folder=slug(robot.name),
        timezone_name=robot.timezone, model=_safe(lambda: robot.model),
        camera_device_id=(_safe(lambda: robot.camera_metadata) or {}).get("deviceId"),
        raw=robot.to_dict(),
    )
    lr_db.add_snapshot(
        db, robot_serial=robot.serial,
        litter_level=_safe(lambda: robot.litter_level),
        waste_drawer_level=_safe(lambda: robot.waste_drawer_level),
        scale_weight=_safe(lambda: robot.pet_weight),
        cycle_count=_safe(lambda: robot.cycle_count),
        status=getattr(status, "text", None) or (str(status) if status else None),
        hopper_status=getattr(_safe(lambda: robot.hopper_status), "name", None),
    )
    lr_db.set_robot_info(db, robot.serial, robot_info(robot))


async def sync_activities(db, robot: LitterRobot5, max_pages: int) -> int:
    """Page back through the activity log. The first run backfills as far as the
    API allows (up to max_pages); later runs stop at the first page with nothing new."""
    key = f"activities_backfilled:{robot.serial}"
    backfilled = lr_db.get_state(db, key) == "1"
    total_new, offset, prev_ids = 0, 0, None
    for _ in range(max_pages):
        items = await robot.get_activities(limit=ACTIVITY_PAGE, offset=offset)
        if not items:
            break
        new, ids = lr_db.upsert_activities(db, robot.serial, items)
        db.commit()
        total_new += new
        if ids == prev_ids:  # API ignored the offset; nothing further to page through
            break
        prev_ids = ids
        if (backfilled and new == 0) or len(items) < ACTIVITY_PAGE:
            break
        offset += ACTIVITY_PAGE
    lr_db.set_state(db, key, "1")
    db.commit()
    return total_new


async def save_pet_photo(db, http: aiohttp.ClientSession, photos: Path, pet_id: str, url: str | None) -> None:
    """Keep a local copy of the cat's profile photo; Whisker's links expire within hours.
    The URL path (without the signature) changes only when the photo does."""
    if not url:
        return
    key = url.split("?", 1)[0]
    row = db.execute("SELECT photo_file, photo_key FROM pets WHERE pet_id = ?", (pet_id,)).fetchone()
    if row and row["photo_key"] == key and row["photo_file"] and (photos / row["photo_file"]).exists():
        return
    async with http.get(url) as resp:
        resp.raise_for_status()
        ext = lr_db.PHOTO_TYPES.get(resp.content_type)
        if not ext:
            raise RuntimeError(f"unexpected content type {resp.content_type}")
        # StreamReader.read(n) returns whatever has arrived so far, so read until EOF
        data = b""
        while chunk := await resp.content.read(15 * 1024 * 1024 + 1 - len(data)):
            data += chunk
            if len(data) > 15 * 1024 * 1024:
                raise RuntimeError("photo larger than 15 MB")
    photos.mkdir(parents=True, exist_ok=True)
    name = f"{slug(pet_id)}{ext}"
    for old in photos.glob(f"{slug(pet_id)}.*"):
        old.unlink(missing_ok=True)
    (photos / name).write_bytes(data)
    db.execute("UPDATE pets SET photo_file = ?, photo_key = ? WHERE pet_id = ?", (name, key, pet_id))
    db.commit()
    LOG.info("Saved profile photo for pet %s", pet_id)


async def sync_pets(db, account: Account, photos: Path) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        for pet in account.pets:
            await _sync_pet(db, http, photos, pet)
    LOG.info("Synced %d pet(s)", len(account.pets))


async def _sync_pet(db, http: aiohttp.ClientSession, photos: Path, pet) -> None:
    try:
        await pet.fetch_weight_history(limit=1000)
    except Exception as err:
        LOG.debug("weight history failed for %s: %s", pet.name, err)
    data = dict(pet.to_dict())
    data["weightHistory"] = [
        {"timestamp": w.timestamp.isoformat(), "weight": w.weight} for w in pet.weight_history
    ]
    lr_db.upsert_pet(db, data)
    db.commit()
    try:
        await save_pet_photo(db, http, photos, pet.id, pet.image_url)
    except Exception as err:
        LOG.warning("profile photo for %s failed: %s", pet.name, err)


async def sync_metadata(account: Account, robots: list[LitterRobot5], args: argparse.Namespace) -> None:
    db = args.db_conn
    try:
        await sync_pets(db, account, lr_db.photo_dir(args.db))
    except Exception:
        LOG.exception("pet sync failed")
    for robot in robots:
        try:
            sync_robot(db, robot)
            db.commit()
            new = await sync_activities(db, robot, args.activity_pages)
            LOG.info("%s: %d new activit%s", robot.name, new, "y" if new == 1 else "ies")
            if robot.has_camera:
                events = await robot.get_camera_client().get_events(limit=500)
                found = lr_db.upsert_detections(db, robot.serial, events)
                db.commit()
                LOG.info("%s: %d new camera detection(s)", robot.name, found)
        except Exception:
            LOG.exception("%s: metadata sync failed", robot.name)


async def run_once(account: Account, args: argparse.Namespace) -> None:
    await account.connect(
        username=os.environ["LR_USERNAME"],
        password=os.environ["LR_PASSWORD"],
        load_robots=True,
        load_pets=True,
    )
    robots = [r for r in account.robots if isinstance(r, LitterRobot5) and r.has_camera]
    if not robots:
        LOG.error("No Litter-Robot 5 Pro with a camera found on this account")
        return
    if not args.dump:
        await sync_metadata(account, robots, args)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
        for robot in robots:
            got, failed = await process_robot(robot, http, args)
            if not args.dump:
                LOG.info("%s: %d new, %d failed", robot.name, got, failed)
    if not args.dump:
        added = lr_db.index_clip_files(args.db_conn, Path(args.out))
        args.db_conn.commit()
        if added:
            LOG.info("Indexed %d clip(s) from disk", added)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="clips", help="output folder (default: ./clips)")
    p.add_argument("--days", type=int, default=7, help="days back to fetch (default 7)")
    p.add_argument("--tz", default="UTC", help="fallback timezone for filenames")
    p.add_argument("--no-thumbs", action="store_true", help="skip thumbnail JPGs")
    p.add_argument("--dump", metavar="DIR", help="write raw API JSON for exploration instead of downloading")
    p.add_argument("--interval", type=int,
        default=int(os.environ.get("LR_DOWNLOAD_INTERVAL", "3600")),
        help="poll for new videos every N seconds (default 3600, 0 = run once); env: LR_DOWNLOAD_INTERVAL. "
             "The Settings page's sync interval overrides this")
    p.add_argument("--update-interval", type=int,
        default=int(os.environ.get("PYBOT_UPDATE_INTERVAL", "86400")),
        help="check for pylitterbot updates every N seconds (default 86400); env: PYBOT_UPDATE_INTERVAL")
    p.add_argument("--db", default=str(lr_db.DB_PATH),
        help=f"SQLite database for metadata (default {lr_db.DB_PATH}); env: DB_PATH")
    p.add_argument("--activity-pages", type=int,
        default=int(os.environ.get("LR_ACTIVITY_MAX_PAGES", "50")),
        help="max pages of 100 activities to fetch per run (default 50); env: LR_ACTIVITY_MAX_PAGES")
    p.add_argument("--retention-days", type=int,
        default=int(os.environ.get("LR_RETENTION_DAYS", "14")),
        help="delete clips older than N days unless marked keep (default 14, 0 = never); env: LR_RETENTION_DAYS. "
             "The Settings page's choice overrides this")
    p.add_argument("--retention-grace-hours", type=float,
        default=float(os.environ.get("LR_RETENTION_GRACE_HOURS", "24")),
        help="wait this long after retention is first enabled before deleting (default 24); env: LR_RETENTION_GRACE_HOURS")
    p.add_argument("--web", action=argparse.BooleanOptionalAction,
        default=os.environ.get("LR_WEB", "1") != "0",
        help="also serve the web UI on port 2287 from this process (default on); env: LR_WEB")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    if args.dump:
        args.web = False

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=lr_db.LOG_FORMAT,
    )
    web = None
    if args.web:
        import web  # Flask is only loaded when the UI runs here
    try:
        handler = lr_db.log_file_handler("sync")
        if web:
            handler.addFilter(lambda r: r.name.split(".")[0] not in web.LOGGERS)
        logging.getLogger().addHandler(handler)
    except OSError as err:
        LOG.warning("Not writing a log file (Settings > Logs will be empty): %s", err)
    for var in ("LR_USERNAME", "LR_PASSWORD"):
        if not os.environ.get(var):
            LOG.error("%s is not set", var)
            return 2
    if args.dump:
        args.interval = 0  # a dump is a one-off, even in the container where LR_DOWNLOAD_INTERVAL is set
    if not shutil.which("ffmpeg") and not args.dump:
        LOG.error("ffmpeg is required")
        return 2

    args.db_conn = db = lr_db.connect(args.db)
    if args.retention_days > 0 and args.retention_days < args.days + 2:
        # anything younger would still be inside the download window and get re-fetched
        LOG.warning("LR_RETENTION_DAYS=%d is shorter than the download window; using %d",
                    args.retention_days, args.days + 2)
        args.retention_days = args.days + 2
    # the Settings page's retention choice overrides this default; neither may go below the window
    lr_db.set_state(db, "retention_default", str(args.retention_days))
    lr_db.set_state(db, "retention_min_days", str(args.days + 2))
    lr_db.set_state(db, "sync_running", "0")
    db.execute("UPDATE whisker_pushes SET status = 'pending' WHERE status = 'sending'")  # interrupted last time
    db.execute("UPDATE robot_commands SET status = 'error', error = 'Interrupted by a restart', updated_at = ? "
               "WHERE status IN ('pending', 'sending')", (time.time(),))
    db.commit()

    # One wake-up event per waiting loop, so both notice a web UI request.
    sync_wake = outbox_wake = None
    if web:
        sync_wake, outbox_wake = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        lr_db.set_waker(lambda: (loop.call_soon_threadsafe(sync_wake.set),
                                 loop.call_soon_threadsafe(outbox_wake.set)))
        threading.Thread(target=serve_web, args=(web,), name="web", daemon=True).start()

    account = Account()
    worker = None if args.dump else asyncio.create_task(outbox_worker(args, account, outbox_wake))
    last_update_check: datetime | None = None
    trigger = "startup"
    try:
        while True:
            now = datetime.now(timezone.utc)
            if last_update_check is None or (now - last_update_check).total_seconds() >= args.update_interval:
                check_and_update_pylitterbot()
                last_update_check = datetime.now(timezone.utc)
            lr_db.set_state(db, "sync_running", "1")
            lr_db.set_state(db, "sync_started", str(time.time()))
            lr_db.set_state(db, "sync_trigger", trigger)
            db.commit()
            error = ""
            try:
                await run_once(account, args)
            except Exception as err:
                LOG.exception("run failed")
                error = str(err)[:300] or type(err).__name__
            if not args.dump:
                try:
                    await apply_retention(db, args)
                except Exception:
                    LOG.exception("retention failed")
                try:
                    await asyncio.to_thread(unassign_ignored)
                except Exception:
                    LOG.exception("unassigning ignored visits failed")
                try:
                    await asyncio.to_thread(lr_notify.after_sync)
                except Exception:
                    LOG.exception("notifications failed")
            lr_db.set_state(db, "sync_error", error)
            lr_db.set_state(db, "sync_finished", str(time.time()))
            lr_db.set_state(db, "sync_running", "0")
            db.commit()
            lr_db.wake()  # requests queued before the first login can be sent now
            if not args.interval:
                return 1 if error else 0
            trigger = await wait_for_next_run(db, args.interval, sync_wake)
    finally:
        if worker:
            worker.cancel()
        await account.disconnect()


def unassign_ignored() -> None:
    """Queue removing the cat Whisker gave to visits too light to be a cat (see web.mark_ignored)."""
    import web  # the visit rules live with the web UI
    with web.app.app_context():
        n = web.queue_ignored_unassigns()
    if n:
        LOG.info("Removing the cat from %d visit(s) under %.1f lb in Whisker", n, web.IGNORE_UNDER_LB)


SMALL_HEIGHT = 360        # small copies: 360p H.264, about 5% of the original's size
RETENTION_BUDGET_S = 600  # stop re-encoding after this long; the rest wait for the next sync


async def shrink_clip(path: Path) -> None:
    """Re-encode a clip in place as a small copy."""
    partial = path.with_name(f"{path.stem}.{os.urandom(4).hex()}.part.mp4")
    await run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                      "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale=-2:{SMALL_HEIGHT},format=yuv420p",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "48k",
                      "-movflags", "+faststart", str(partial)], partial)
    partial.replace(path)


async def save_picture(path: Path, thumb: Path) -> None:
    """A still from the clip, for clips whose Whisker thumbnail never downloaded."""
    partial = thumb.with_name(f"{thumb.stem}.{os.urandom(4).hex()}.part.jpg")
    await run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1", "-i", str(path),
                      "-frames:v", "1", "-vf", "scale=-2:480", "-q:v", "4", str(partial)], partial)
    partial.replace(thumb)


async def apply_retention(db, args: argparse.Namespace) -> None:
    """Clips older than the full-quality period become small copies (or, without a small
    copy stage, lose their video); small copies past their age lose their video too. A
    clip that loses its video keeps its picture. Clips marked keep are left alone."""
    full, small = lr_db.retention_tiers(db)
    if not full:
        return
    # Give a freshly enabled or shortened policy a grace period so existing clips
    # can be marked "keep" before anything changes.
    enabled_at = lr_db.get_state(db, "retention_enabled_at")
    if not enabled_at:
        enabled_at = str(time.time())
        lr_db.set_state(db, "retention_enabled_at", enabled_at)
        db.commit()
    starts = float(enabled_at) + args.retention_grace_hours * 3600
    if time.time() < starts:
        LOG.info("Retention: changes to clips older than %d days start %s",
                 full, datetime.fromtimestamp(starts).strftime("%Y-%m-%d %H:%M"))
        return
    now, root = time.time(), Path(args.out)
    small_until = None if small is None else (0 if small == 0 else now - small * 86400)

    async def to_picture(r) -> bool:
        mp4 = root / r["robot_folder"] / r["video_file"]
        thumb = mp4.with_suffix(".jpg")
        if not thumb.exists() and mp4.exists():
            try:
                await save_picture(mp4, thumb)
            except RuntimeError as err:
                LOG.warning("retention: no picture saved for %s: %s", r["video_file"], err)
        return lr_db.drop_clip_video(db, root, r, LOG)

    shrunk = dropped = saved = 0
    deadline = time.monotonic() + RETENTION_BUDGET_S
    rows = db.execute("SELECT id, robot_folder, video_file, ts FROM clips WHERE keep = 0 AND deleted_at IS NULL "
                      "AND video_file IS NOT NULL AND quality IS NULL AND ts < ? ORDER BY ts DESC",
                      (now - full * 86400,)).fetchall()
    for r in rows:
        if small_until is not None and r["ts"] >= small_until:
            if time.monotonic() > deadline:
                LOG.info("Retention: time's up for this sync; the rest are shrunk next time")
                break
            mp4 = root / r["robot_folder"] / r["video_file"]
            try:
                before = mp4.stat().st_size
                await shrink_clip(mp4)
            except (OSError, RuntimeError) as err:
                LOG.warning("retention: could not shrink %s: %s", r["video_file"], err)
                continue
            db.execute("UPDATE clips SET quality = 'small' WHERE id = ?", (r["id"],))
            db.commit()
            shrunk += 1
            saved += before - mp4.stat().st_size
        elif await to_picture(r):
            dropped += 1
    if small:
        for r in db.execute("SELECT id, robot_folder, video_file, ts FROM clips WHERE keep = 0 AND deleted_at IS NULL "
                            "AND video_file IS NOT NULL AND quality = 'small' AND ts < ?",
                            (now - small * 86400,)).fetchall():
            if await to_picture(r):
                dropped += 1
    db.commit()
    if shrunk:
        LOG.info("Retention: shrank %d clip(s) older than %d days, saving %.0f MB", shrunk, full, saved / 1e6)
    if dropped:
        LOG.info("Retention: removed the video from %d clip(s), keeping their pictures", dropped)


async def send_pushes(db, account: Account) -> None:
    """Send cat reassignments queued by the web UI to Whisker."""
    robots = {r.serial: r for r in account.robots if isinstance(r, LitterRobot5)}
    if not robots:  # not logged in yet; try again after the next connect
        return
    rows = db.execute(
        "SELECT p.id, p.activity_id, p.to_pet, a.ts, a.robot_serial, a.pet_ids, a.raw FROM whisker_pushes p "
        "JOIN activities a ON a.id = p.activity_id WHERE p.status = 'pending' ORDER BY p.id"
    ).fetchall()
    for p in rows:
        cur = db.execute("UPDATE whisker_pushes SET status = 'sending', updated_at = ? "
                         "WHERE id = ? AND status = 'pending'", (time.time(), p["id"]))
        db.commit()
        if not cur.rowcount:  # superseded by a newer request
            continue
        current, target = lr_db.first_pet(p["pet_ids"]), p["to_pet"]
        error = None
        if current != target and not lr_db.whisker_editable(p["ts"]):
            error = "Whisker only accepts changes to visits from the last 7 days"
        elif current != target:
            event_id = json.loads(p["raw"] or "{}").get("eventId")
            robot = robots.get(p["robot_serial"])
            if not event_id:
                error = "This visit has no eventId, so Whisker can't update it"
            elif not robot:
                error = "Robot not found on this account"
            else:
                try:
                    result = await robot.reassign_pet_visit(
                        event_id, from_pet_id=current, to_pet_id=target)
                except Exception as err:
                    result, error = None, str(err)[:200]
                if result is None:
                    error = error or "Whisker rejected the change"
        lr_db.finish_push(db, p["id"], p["activity_id"], target, error)
        if error:
            LOG.warning("Whisker update for visit %s failed: %s", p["activity_id"], error)
        else:
            LOG.info("Whisker: visit %s now assigned to %s", p["activity_id"], target or "no cat")


async def redownload_clip(db, robot: LitterRobot5, args: argparse.Namespace, clip_id: str) -> str | None:
    """Fetch a fresh copy of one clip from Whisker. download_clip writes to a temp
    file and only then replaces the old one, so a failure leaves the old copy intact.
    Returns an error message, or None on success."""
    row = db.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if not row:
        return "Clip not found"
    raw = json.loads(row["raw"] or "{}")
    raw.setdefault("createdAt", row["ts"])  # upsert_clip keys the clip's time off this
    folder = Path(args.out) / row["robot_folder"]
    if row["video_file"]:
        stem = Path(row["video_file"]).stem
    else:
        when = datetime.fromtimestamp(row["ts"], ZoneInfo(robot.timezone or args.tz))
        stem = f"{slug(robot.name)}_{when:%Y-%m-%d}_{when:%H-%M-%S}"
    folder.mkdir(parents=True, exist_ok=True)
    mp4, thumb = folder / f"{stem}.mp4", folder / f"{stem}.jpg"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
        try:
            playback = await download_clip(robot.get_camera_client(), http, clip_id, mp4)
        except ClipNotReady:
            return "Whisker hasn't finished storing this clip yet; try again in a few minutes"
        if playback is None:
            return "Whisker no longer has this clip (it may have expired from Whisker's history)"
        (folder / f"{stem}.json").write_text(json.dumps(raw, indent=2))
        if raw.get("videoThumbnail") and not args.no_thumbs:
            try:
                thumb.write_bytes(await fetch(http, raw["videoThumbnail"], playback.cookie_header))
            except Exception as err:
                LOG.debug("thumbnail failed for %s: %s", clip_id, err)
    lr_db.upsert_clip(db, clip_id=clip_id, robot_serial=robot.serial, robot_folder=row["robot_folder"], raw=raw,
                      video_file=mp4.name, thumb_file=thumb.name if thumb.exists() else None)
    db.execute("UPDATE clips SET deleted_at = NULL, quality = NULL WHERE id = ?", (clip_id,))
    db.commit()
    return None


async def run_commands(db, account: Account, args: argparse.Namespace) -> None:
    """Run robot commands (clean, reset, ...) and clip re-downloads queued by the web UI."""
    # a clean cycle firing long after the click would be a surprise, so stale requests
    # expire; downloads can legitimately queue behind each other for longer
    now = time.time()
    db.execute("UPDATE robot_commands SET status = 'error', error = 'Not sent: the collector was not ready in time', "
               "updated_at = ? WHERE status = 'pending' AND ((command != 'redownload' AND created_at < ?) "
               "OR (command = 'redownload' AND created_at < ?))", (now, now - 120, now - 1800))
    db.commit()
    robots = {r.serial: r for r in account.robots if isinstance(r, LitterRobot5)}
    if not robots:
        return
    for c in db.execute("SELECT * FROM robot_commands WHERE status = 'pending' ORDER BY id").fetchall():
        robot = robots.get(c["robot_serial"])
        db.execute("UPDATE robot_commands SET status = 'sending', updated_at = ? WHERE id = ?", (time.time(), c["id"]))
        db.commit()
        error = None
        if not robot:
            error = "Robot not found on this account"
        elif c["command"] == "redownload":
            try:
                error = await redownload_clip(db, robot, args, c["arg"])
            except Exception as err:
                error = (str(err) or type(err).__name__)[:200]
        elif c["command"] not in lr_db.ROBOT_COMMANDS:
            error = f"Unknown command {c['command']}"
        else:
            method = lr_db.ROBOT_COMMANDS[c["command"]]
            try:
                if method and not await getattr(robot, method)():
                    error = "Whisker rejected the command"
            except Exception as err:
                error = str(err)[:200] or type(err).__name__
            try:
                if method:
                    await asyncio.sleep(2)  # give the robot a moment to report its new state
                await robot.refresh()
                lr_db.set_robot_info(db, robot.serial, robot_info(robot))
            except Exception as err:
                LOG.debug("refresh after %s failed: %s", c["command"], err)
        db.execute("UPDATE robot_commands SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                   ("error" if error else "done", error, time.time(), c["id"]))
        db.commit()
        if c["command"] != "refresh":
            if error:
                LOG.warning("%s: %s failed: %s", c["robot_serial"], c["command"], error)
            else:
                LOG.info("%s: %s sent", c["robot_serial"], c["command"])


async def outbox_worker(args: argparse.Namespace, account: Account, wake: asyncio.Event | None) -> None:
    """Background loop that acts on requests from the web UI (robot commands, clip
    re-downloads and Whisker cat corrections) within a couple of seconds, even mid-sync."""
    db = lr_db.connect(args.db)
    while True:
        try:
            if lr_db.has_pending_commands(db):
                await run_commands(db, account, args)
            if lr_db.has_pending_pushes(db):
                await send_pushes(db, account)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("handling web UI requests failed")
        await wait_for_wake(wake, 60, poll=1.5)


async def wait_for_next_run(db, default_interval: int, wake: asyncio.Event | None) -> str:
    """Sleep until the next scheduled run, waking early for a manual refresh from the web UI.
    The interval counts from when the last sync started, so syncs keep to the schedule
    however long each takes. It's re-read while waiting, so a change on the Settings page
    applies at once."""
    while True:
        started = float(lr_db.get_state(db, "sync_started") or 0)
        requested = float(lr_db.get_state(db, "refresh_requested") or 0)
        if requested > started:
            LOG.info("Manual refresh requested")
            return "manual"
        remaining = started + lr_db.sync_interval(db, default_interval) - time.time()
        if remaining <= 0:
            return "scheduled"
        await wait_for_wake(wake, max(0.1, remaining), poll=2)


async def wait_for_wake(wake: asyncio.Event | None, timeout: float, poll: float) -> None:
    """Sleep until the in-process web UI queues something, or for at most timeout.
    Without the UI in this process nothing can wake us, so check back every poll seconds."""
    if wake is None:
        await asyncio.sleep(min(poll, timeout))
        return
    try:
        await asyncio.wait_for(wake.wait(), timeout)
    except TimeoutError:
        pass
    wake.clear()  # the caller re-reads the database next, so nothing set before this is missed


def serve_web(web) -> None:
    try:
        web.serve()
    except BaseException:
        LOG.exception("Web UI stopped")
        os._exit(1)  # let the container restart rather than run on without a UI


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
