#!/usr/bin/env python3
"""Download recorded Litter-Robot 5 Pro camera clips from the Whisker cloud.

Uses pylitterbot (>= 2025.6.5) for login and the camera inventory API, then
fetches each clip's HLS stream with its short-lived CloudFront signed cookies
and remuxes it to MP4 with ffmpeg.

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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
from pylitterbot import Account
from pylitterbot.robot.litterrobot5 import LitterRobot5

import lr_db

LOG = logging.getLogger("lr_clips")
ACTIVITY_PAGE = 100
URI_ATTR = re.compile(r'URI="([^"]+)"')
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


# --------------------------------------------------------------------------- HLS


async def fetch(http: aiohttp.ClientSession, url: str, cookie: str) -> bytes:
    async with http.get(url, headers={"Cookie": cookie}) as resp:
        resp.raise_for_status()
        return await resp.read()


def pick_variant(master: str, base: str) -> str | None:
    """Return the highest-bandwidth variant URL from a master playlist."""
    best, best_bw, lines = None, -1, master.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(bw.group(1)) if bw else 0
            nxt = next((l for l in lines[i + 1 :] if l and not l.startswith("#")), None)
            if nxt and bw > best_bw:
                best, best_bw = urljoin(base, nxt), bw
    return best


async def download_hls(
    http: aiohttp.ClientSession, playlist_url: str, cookie: str, dest: Path
) -> None:
    """Mirror an HLS playlist locally (segments, init map, keys), then remux to MP4.

    Fetching everything ourselves guarantees the CloudFront cookie goes on every
    request; ffmpeg then only ever reads local files.
    """
    text = (await fetch(http, playlist_url, cookie)).decode()
    if "#EXT-X-STREAM-INF" in text:
        variant = pick_variant(text, playlist_url)
        if not variant:
            raise RuntimeError("master playlist had no variants")
        playlist_url = variant
        text = (await fetch(http, playlist_url, cookie)).decode()

    with tempfile.TemporaryDirectory(prefix="lrhls-") as tmp:
        tmpdir = Path(tmp)
        counter = 0
        sem = asyncio.Semaphore(6)
        jobs: list[asyncio.Task] = []

        def local_name(remote: str) -> str:
            nonlocal counter
            counter += 1
            ext = Path(remote.split("?")[0]).suffix or ".bin"
            name = f"part{counter:05d}{ext}"

            async def grab() -> None:
                async with sem:
                    data = await fetch(http, urljoin(playlist_url, remote), cookie)
                (tmpdir / name).write_bytes(data)

            jobs.append(asyncio.create_task(grab()))
            return name

        out_lines = []
        for line in text.splitlines():
            if line.startswith("#"):
                line = URI_ATTR.sub(lambda m: f'URI="{local_name(m.group(1))}"', line)
            elif line.strip():
                line = local_name(line.strip())
            out_lines.append(line)

        await asyncio.gather(*jobs)
        local_m3u8 = tmpdir / "index.m3u8"
        local_m3u8.write_text("\n".join(out_lines) + "\n")

        partial = dest.with_suffix(".part.mp4")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,crypto",
            "-i", str(local_m3u8),
            "-c", "copy", "-movflags", "+faststart",
            str(partial),
        ]
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")
        partial.rename(dest)


# ------------------------------------------------------------------------ clips


def date_window(days: int) -> list[str]:
    """Dates to query. The API's date bucketing (UTC vs local) is unconfirmed,
    so cover one extra day either side and dedupe by clip id."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(d)).isoformat() for d in range(-1, days + 1)]


async def list_clips(robot: LitterRobot5, days: int) -> list:
    seen: dict[str, object] = {}
    for clip in await robot.get_camera_videos():  # newest, no date filter
        seen.setdefault(clip.id, clip)
    for day in date_window(days):
        for clip in await robot.get_camera_videos(date=day):
            seen.setdefault(clip.id, clip)
    return sorted(seen.values(), key=lambda c: c.created_at or datetime.min.replace(tzinfo=timezone.utc))


async def process_robot(
    robot: LitterRobot5, http: aiohttp.ClientSession, args: argparse.Namespace
) -> tuple[int, int]:
    tz = ZoneInfo(robot.timezone or args.tz)
    client = robot.get_camera_client()
    clips = await list_clips(robot, args.days)
    LOG.info("%s: %d clip(s) listed", robot.name, len(clips))

    if args.dump:
        d = Path(args.dump) / slug(robot.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "robot.json").write_text(json.dumps(robot.to_dict(), indent=2, default=str))
        (d / "clips.json").write_text(json.dumps([c.raw for c in clips], indent=2))
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
            continue
        if lr_db.is_clip_deleted(db, clip.id):  # removed by retention; don't fetch it again
            continue
        lr_db.upsert_clip(db, clip_id=clip.id, robot_serial=robot.serial,
                          robot_folder=folder.name, raw=clip.raw)
        folder.mkdir(parents=True, exist_ok=True)

        playback = await client.get_video_playback(clip.id)  # fresh cookies per clip
        url = (playback and playback.hls_url) or clip.raw.get("hlsUrl")
        if not playback or not url:
            LOG.warning("%s: no playback for clip %s", robot.name, clip.id)
            failed += 1
            continue
        try:
            await download_hls(http, url, playback.cookie_header, mp4)
        except Exception as err:  # keep going; one bad clip shouldn't stop the run
            LOG.warning("%s: clip %s failed: %s", robot.name, clip.id, err)
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
    )


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


async def sync_pets(db, account: Account) -> None:
    for pet in account.pets:
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
    LOG.info("Synced %d pet(s)", len(account.pets))


async def sync_metadata(account: Account, robots: list[LitterRobot5], args: argparse.Namespace) -> None:
    db = args.db_conn
    try:
        await sync_pets(db, account)
    except Exception:
        LOG.exception("pet sync failed")
    for robot in robots:
        try:
            sync_robot(db, robot)
            db.commit()
            new = await sync_activities(db, robot, args.activity_pages)
            LOG.info("%s: %d new activit%s", robot.name, new, "y" if new == 1 else "ies")
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
        help="poll for new videos every N seconds (default 3600, 0 = run once); env: LR_DOWNLOAD_INTERVAL")
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
        help="delete clips older than N days unless marked keep (default 14, 0 = never); env: LR_RETENTION_DAYS")
    p.add_argument("--retention-grace-hours", type=float,
        default=float(os.environ.get("LR_RETENTION_GRACE_HOURS", "24")),
        help="wait this long after retention is first enabled before deleting (default 24); env: LR_RETENTION_GRACE_HOURS")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for var in ("LR_USERNAME", "LR_PASSWORD"):
        if not os.environ.get(var):
            LOG.error("%s is not set", var)
            return 2
    if not shutil.which("ffmpeg") and not args.dump:
        LOG.error("ffmpeg is required")
        return 2

    args.db_conn = db = lr_db.connect(args.db)
    if args.retention_days > 0 and args.retention_days < args.days + 2:
        # anything younger would still be inside the download window and get re-fetched
        LOG.warning("LR_RETENTION_DAYS=%d is shorter than the download window; using %d",
                    args.retention_days, args.days + 2)
        args.retention_days = args.days + 2
    lr_db.set_state(db, "retention_days", str(args.retention_days))
    lr_db.set_state(db, "sync_running", "0")
    db.commit()

    account = Account()
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
                    apply_retention(db, args)
                except Exception:
                    LOG.exception("retention failed")
            lr_db.set_state(db, "sync_error", error)
            lr_db.set_state(db, "sync_finished", str(time.time()))
            lr_db.set_state(db, "sync_running", "0")
            db.commit()
            if not args.interval:
                return 1 if error else 0
            trigger = await wait_for_next_run(db, args.interval)
    finally:
        await account.disconnect()


def apply_retention(db, args: argparse.Namespace) -> None:
    if args.retention_days <= 0:
        return
    # Give a freshly enabled retention policy a grace period so existing clips
    # can be marked "keep" before anything is removed.
    enabled_at = lr_db.get_state(db, "retention_enabled_at")
    if not enabled_at:
        enabled_at = str(time.time())
        lr_db.set_state(db, "retention_enabled_at", enabled_at)
        db.commit()
    starts = float(enabled_at) + args.retention_grace_hours * 3600
    if time.time() < starts:
        LOG.info("Retention: deleting clips older than %d days starts %s",
                 args.retention_days, datetime.fromtimestamp(starts).strftime("%Y-%m-%d %H:%M"))
        return
    deleted = lr_db.apply_retention(db, Path(args.out), args.retention_days, LOG)
    if deleted:
        LOG.info("Retention: deleted %d clip(s) older than %d days", deleted, args.retention_days)


async def wait_for_next_run(db, interval: int) -> str:
    """Sleep until the next scheduled run, waking early for a manual refresh from the web UI."""
    deadline = time.monotonic() + interval
    while time.monotonic() < deadline:
        requested = float(lr_db.get_state(db, "refresh_requested") or 0)
        if requested > float(lr_db.get_state(db, "sync_started") or 0):
            LOG.info("Manual refresh requested")
            return "manual"
        await asyncio.sleep(min(2, max(0.1, deadline - time.monotonic())))
    return "scheduled"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
