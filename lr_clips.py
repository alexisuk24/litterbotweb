#!/usr/bin/env python3
"""Download recorded Litter-Robot 5 Pro camera clips from the Whisker cloud.

Uses pylitterbot (>= 2025.6.5) for login and the camera inventory API, then
fetches each clip's HLS stream with its short-lived CloudFront signed cookies
and remuxes it to MP4 with ffmpeg.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
from pylitterbot import Account
from pylitterbot.robot.litterrobot5 import LitterRobot5

LOG = logging.getLogger("lr_clips")
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
        if clips:
            pb = await client.get_video_playback(clips[-1].id)
            if pb:
                (d / "playback_sample.json").write_text(json.dumps(pb.raw, indent=2))
        LOG.info("%s: API dump written to %s", robot.name, d)
        return 0, 0

    got = failed = 0
    for clip in clips:
        when = (clip.created_at or datetime.now(timezone.utc)).astimezone(tz)
        folder = Path(args.out) / slug(robot.name)
        stem = f"{slug(robot.name)}_{when:%Y-%m-%d}_{when:%H-%M-%S}"
        mp4 = folder / f"{stem}.mp4"
        if mp4.exists():
            continue
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
                (folder / f"{stem}.jpg").write_bytes(
                    await fetch(http, clip.thumbnail_url, playback.cookie_header)
                )
            except Exception as err:
                LOG.debug("thumbnail failed for %s: %s", clip.id, err)
        got += 1
        LOG.info("%s: saved %s", robot.name, mp4.relative_to(args.out))
    return got, failed


async def run_once(account: Account, args: argparse.Namespace) -> None:
    await account.connect(
        username=os.environ["LR_USERNAME"],
        password=os.environ["LR_PASSWORD"],
        load_robots=True,
    )
    robots = [r for r in account.robots if isinstance(r, LitterRobot5) and r.has_camera]
    if not robots:
        LOG.error("No Litter-Robot 5 Pro with a camera found on this account")
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
        for robot in robots:
            got, failed = await process_robot(robot, http, args)
            if not args.dump:
                LOG.info("%s: %d new, %d failed", robot.name, got, failed)


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

    account = Account()
    last_update_check: datetime | None = None
    try:
        while True:
            now = datetime.now(timezone.utc)
            if last_update_check is None or (now - last_update_check).total_seconds() >= args.update_interval:
                check_and_update_pylitterbot()
                last_update_check = datetime.now(timezone.utc)
            try:
                await run_once(account, args)
            except Exception:
                LOG.exception("run failed")
                if not args.interval:
                    return 1
            if not args.interval:
                return 0
            await asyncio.sleep(args.interval)
    finally:
        await account.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
