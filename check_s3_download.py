#!/usr/bin/env python3
"""Pre-deploy check for the S3 clip download.

Logs in with LR_USERNAME / LR_PASSWORD, downloads the newest few clips of each
camera robot through lr_clips.download_clip (the same path the sync uses), and
checks each one with ffprobe:

  - codecs of every chunk and of the joined file (the image's ffmpeg is built
    for H.264 + AAC only), and that clips whose chunks change resolution were
    re-encoded at the largest one, keeping every frame
  - joined duration vs. the sum of the chunks and vs. Whisker's hlsDuration
  - timestamp gaps or jumps backwards where chunks meet
  - the thumbnail still downloads with the CloudFront cookies

Clips and chunks are kept in --out so you can watch them. Exits 1 if any clip
fails. Doesn't touch the database or the clips folder.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import aiohttp
from pylitterbot import Account
from pylitterbot.robot.litterrobot5 import LitterRobot5

import lr_clips
import lr_db

EXPECTED_CODECS = {"h264", "aac"}
DURATION_TOLERANCE_S = 1.5


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=index,codec_type,codec_name,width,height",
         "-of", "json", str(path)], capture_output=True, text=True)
    data = json.loads(out.stdout or "{}")
    frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                             "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return {"duration": float(data.get("format", {}).get("duration") or 0),
            "frames": int(frames.stdout.strip() or 0),
            "codecs": sorted({s["codec_name"] for s in data.get("streams", [])}),
            "size": next((f"{s['width']}x{s['height']}" for s in data.get("streams", [])
                          if s.get("codec_type") == "video"), "?"),
            "errors": out.stderr.strip()}


def timestamp_problems(path: Path) -> list[str]:
    """Gaps and backwards jumps in packet timestamps, per stream."""
    problems = []
    for sel in ("v", "a"):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"{sel}:0", "-show_entries",
             "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        rows = []
        for line in out.stdout.splitlines():
            parts = line.split(",")
            try:
                rows.append((float(parts[0]), float(parts[1]) if len(parts) > 1 and parts[1] else 0.0))
            except ValueError:
                continue
        rows.sort(key=lambda r: r[0])  # B-frames arrive out of order; compare in presentation order
        name = "video" if sel == "v" else "audio"
        typical = sorted(d for _, d in rows if d > 0)
        step = typical[len(typical) // 2] if typical else 0.1
        for (a, _), (b, _) in zip(rows, rows[1:]):
            if b - a > max(3 * step, 0.25):
                problems.append(f"{name} gap of {b - a:.2f}s at {a:.2f}s")
            elif b == a:
                problems.append(f"{name} duplicate timestamp at {a:.2f}s")
    return problems[:5]


async def check_clip(client, http: aiohttp.ClientSession, clip, out: Path) -> bool:
    print(f"\n== clip {clip.id}  ({clip.created_at:%Y-%m-%d %H:%M:%S %Z})" if clip.created_at else f"\n== clip {clip.id}")
    out.mkdir(parents=True, exist_ok=True)
    mp4 = out / f"{clip.id}.mp4"
    ok = True
    try:
        playback = await lr_clips.download_clip(client, http, clip.id, mp4)
    except lr_clips.ClipNotReady:
        print("   SKIP  no chunks yet (Whisker is still storing it)")
        return True
    except Exception as err:
        print(f"   FAIL  download: {type(err).__name__}: {err}")
        return False
    if playback is None:
        print("   FAIL  clip detail request failed")
        return False

    # the chunks on their own, through the library's own download method
    chunk_dir = out / f"{clip.id}_chunks"
    chunk_paths = await client.download_video(clip.id, chunk_dir) or []
    chunk_info = [probe(p) for p in chunk_paths]
    for p, info in zip(chunk_paths, chunk_info):
        print(f"   chunk {p.name}: {info['duration']:.2f}s {info['size']} {','.join(info['codecs'])}")

    joined = probe(mp4)
    print(f"   joined: {joined['duration']:.2f}s {joined['size']} {','.join(joined['codecs'])} -> {mp4}")
    if joined["errors"]:
        print(f"   WARN  ffprobe: {joined['errors'][:300]}")

    codecs = set(joined["codecs"]).union(*(set(c["codecs"]) for c in chunk_info))
    if not codecs:
        print("   FAIL  no streams found")
        ok = False
    elif codecs - EXPECTED_CODECS:
        print(f"   FAIL  codec(s) {sorted(codecs - EXPECTED_CODECS)} aren't in the image's ffmpeg build")
        ok = False

    sizes = list(dict.fromkeys(c["size"] for c in chunk_info))
    if len(sizes) > 1:
        biggest = max(sizes, key=lambda s: int(s.split("x")[0]) * int(s.split("x")[1]) if "x" in s else 0)
        if joined["size"] != biggest:
            print(f"   FAIL  chunks change resolution ({' -> '.join(sizes)}) but the joined file is {joined['size']}")
            ok = False
        else:
            print(f"   ok    chunks change resolution ({' -> '.join(sizes)}); re-encoded at {biggest}")

    chunk_total = sum(c["duration"] for c in chunk_info)
    frames_in = sum(c["frames"] for c in chunk_info)
    if chunk_info and joined["frames"] != frames_in:
        print(f"   WARN  joined has {joined['frames']} video frames, the chunks {frames_in}")
    if chunk_info and abs(chunk_total - joined["duration"]) > DURATION_TOLERANCE_S:
        print(f"   FAIL  joined is {joined['duration']:.2f}s but the chunks add up to {chunk_total:.2f}s")
        ok = False
    expected = lr_db.hls_duration_s(clip.raw.get("hlsDuration"))
    if expected is not None:
        if abs(expected - joined["duration"]) > DURATION_TOLERANCE_S:
            print(f"   WARN  Whisker says {expected:.0f}s, joined is {joined['duration']:.2f}s")
        else:
            print(f"   ok    matches Whisker's length ({expected:.0f}s)")

    for problem in timestamp_problems(mp4):
        print(f"   WARN  {problem}")

    if clip.thumbnail_url:
        try:
            thumb = await lr_clips.fetch(http, clip.thumbnail_url, playback.cookie_header)
            (out / f"{clip.id}.jpg").write_bytes(thumb)
            print(f"   ok    thumbnail {len(thumb)} bytes")
        except Exception as err:
            print(f"   FAIL  thumbnail: {err}")
            ok = False
    print("   PASS" if ok else "   FAILED")
    return ok


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--clips", type=int, default=3, help="newest clips to check per robot (default 3)")
    p.add_argument("--out", default="./s3-check", help="where to keep the downloads (default ./s3-check)")
    args = p.parse_args()
    for var in ("LR_USERNAME", "LR_PASSWORD"):
        if not os.environ.get(var):
            print(f"{var} is not set", file=sys.stderr)
            return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    account = Account()
    results = []
    try:
        await account.connect(username=os.environ["LR_USERNAME"], password=os.environ["LR_PASSWORD"],
                              load_robots=True)
        robots = [r for r in account.robots if isinstance(r, LitterRobot5) and r.has_camera]
        if not robots:
            print("No Litter-Robot 5 Pro with a camera on this account", file=sys.stderr)
            return 2
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as http:
            for robot in robots:
                tz = robot.timezone or "UTC"
                clips = await lr_clips.list_clips(robot, 1, tz)
                print(f"\n### {robot.name}: {len(clips)} clip(s) listed in {tz}, checking the newest {args.clips}")
                client = robot.get_camera_client()
                for clip in clips[-args.clips:][::-1]:
                    results.append(await check_clip(client, http, clip, out / lr_clips.slug(robot.name)))
    finally:
        await account.disconnect()

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} clip(s) passed. Files are in {out.resolve()}")
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
