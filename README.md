# Litter-Robot 5 Pro clip downloader

Pulls recorded camera clips from the Whisker cloud for every Litter-Robot 5 Pro on
your account and saves them as MP4 (plus a thumbnail and the raw clip JSON).

It also collects the robot's metadata into a local SQLite database
(`/data/litterrobot.db`): pets and their weight history, the activity log (each pet
visit with weight, duration and waste type, plus clean cycles and offline events),
robot snapshots (litter level, drawer level), and an index of every clip.

## Web UI (port 2287)

- **Events**: every pet visit with the camera clips recorded around it. Click a
  visit to watch its clip(s), see the weight/duration, and fix which cat it was.
  Visits whose weight looks like a different cat (or that have no cat) are flagged
  **Check**; the *Needs review* filter lists just those. Keyboard: `↑/↓` move,
  `1–9` assign cat, `0` unknown, `u` undo.
  Visits also show the camera's own cat recognition (from its AI detections around the
  visit) as a similarity score, e.g. "📷 74%", highlighted with the cat's name when the
  camera thinks it was a different cat.
  Corrections are stored in the local database, and cat changes, "Unknown" and
  reverts are also sent to your Whisker account (switch this off on the **Settings**
  page), which corrects that cat's weight history
  there too. The collector sends them within a few seconds and the page shows
  whether Whisker accepted each one. Whisker only accepts changes to visits from the
  last 7 days, so older corrections are kept locally and not sent. Notes always stay local.
- **Clips**: the calendar browser for downloaded clips.
- **Analysis**: insights (visit-frequency changes, weight changes of 5% or more,
  cats that haven't visited, offline events), per-cat cards, visits per day, weight
  trend, time-of-day and weekday×hour patterns, clean cycles, litter/drawer levels,
  and a table of the robot's most common cat mix-ups based on your corrections.

Analysis uses your corrections.

**Settings** also shows the app logs: the sync collector's log (downloads, activity
sync, Whisker updates) and the web server's warnings and errors, with a
warnings-and-errors filter and a download button. The sync log has the same lines `docker logs`
shows, but kept across container restarts.

**Robots** has a page per robot: status, faults, waste drawer and litter levels,
cycle counts, filter date, Wi-Fi and firmware, plus actions to start a clean cycle,
reset the waste drawer gauge after emptying it, and remote-reset the robot (each asks
for confirmation). Firmware updates aren't possible from here; Whisker's LR5 API has no
endpoint for them, so use the Whisker app. The firmware card shows each component's version and a history of
updates, recorded from when this version of the app first ran (changes are noticed at the
next sync).

**Live view** on a camera robot's page streams the camera over WebRTC, with Front/Globe
switching and sound (muted until you unmute). Video goes straight from the camera to your
browser; the web server only relays the connection setup through Whisker, logging in with
the same `LR_USERNAME` / `LR_PASSWORD`. It stops after 10 minutes, or when you leave or hide
the page.

**↻ Re-download** (under the player on Events and Clips) fetches a fresh copy of a clip
from Whisker if the local one looks wrong; the old file is only replaced once the new
download succeeds. Clips that haven't downloaded yet show **Download now**. This only
works while Whisker still has the clip (about 7 days without Whisker+).

**Settings** has the Whisker sync switch and the weight unit (kg by default, or lb).

**Refresh** (top right on every page) runs a sync immediately instead of waiting for
the next scheduled one. It's disabled while a sync is running and for 5 seconds after
one finishes.

**Retention**: clips older than `LR_RETENTION_DAYS` (14) are deleted after each sync,
except clips marked **📌 Keep forever** (button under the player on the Events and Clips
pages). The first time retention is enabled it waits `LR_RETENTION_GRACE_HOURS` (24)
before deleting anything, so you can mark existing clips first. A banner shows when it
starts. Deleted clips are never re-downloaded; their visit history stays.

Built on [pylitterbot](https://github.com/natekspencer/pylitterbot) 2025.9.0+, which
already reverse-engineered the LR5 Pro camera APIs.

## Run it

```bash
pip install "pylitterbot>=2025.9.0"     # ffmpeg must be on PATH
export LR_USERNAME=you@example.com LR_PASSWORD=...
python lr_clips.py --dump ./api-dump    # first: look at what the API returns
python lr_clips.py --out ./clips        # then: download the last 7 days
```

Or as a container that polls hourly: copy this folder to
`/data/Containers/home/litterrobot-clips/`, create `.env` from `.env.example`, then
`docker compose up -d --build`. Already-downloaded clips are skipped.

Output layout: `clips/<robot name>/<robot name>_<YYYY-MM-DD>_<HH-MM-SS>.mp4`

Volumes: `/clips` (videos) and `/data` (SQLite database, plus each cat's profile photo in
`/data/pet-photos`, copied at sync because Whisker's photo links expire within hours,
and log files in `/data/logs`).
The container runs as uid/gid 1000, so both host folders must be owned by it
(`sudo chown -R 1000:1000 <folder>`); it exits with that hint if they aren't.

Publishing the image (with the SBOM and provenance attestations Docker Scout checks for):

```bash
docker buildx build --pull --platform linux/amd64 \
  --provenance=mode=max --sbom=true \
  -t alexisuk24/lr_video_downloader:latest --push .
```

| Env var | Default | Purpose |
|---|---|---|
| `LR_DOWNLOAD_INTERVAL` | 3600 | Seconds between sync runs (clips and metadata) |
| `LR_ACTIVITY_MAX_PAGES` | 50 | Pages of 100 activities to backfill on the first run |
| `LR_HWACCEL` | auto | Encoder for clips that switch cameras mid-clip (front at 1080p, then globe at 720p), which are re-encoded at 1080p so they don't freeze: `auto` uses VAAPI when `/dev/dri` is mapped in (see `compose.yml`) and the CPU otherwise, `vaapi` warns if no device is found, `off` always uses the CPU |
| `MATCH_BEFORE_S` / `MATCH_AFTER_S` | 600 / 180 | How far before/after a visit a clip still counts as that visit's clip |
| `LR_RETENTION_DAYS` | 14 | Delete clips older than this unless kept (0 = keep everything) |
| `LR_RETENTION_GRACE_HOURS` | 24 | Delay before the first deletions after retention is enabled |
| `DB_PATH` | /data/litterrobot.db | Database location |
| `LOG_DIR` | /data/logs | Where `sync.log` and `web.log` are written (1 MB each, 3 rotated copies kept) |

## How the API works

| Step | Request | Notes |
|---|---|---|
| Login | AWS Cognito via the Whisker app client | pylitterbot handles token refresh |
| Robots | `GET https://ub.prod.iothings.site/robots` | Pro units have `type: LR5_PRO` and `cameraMetadata.deviceId` |
| List clips | `GET https://rrntg65uwf.execute-api.us-east-1.amazonaws.com/prod/v1/cameras/{deviceId}/videos[/{YYYY-MM-DD}]` | Bearer token **and** `x-api-key` (the app's static key, bundled in pylitterbot) |
| Clip detail | `GET .../prod/v1/camera-videos/{clipId}` | Body has `chunks[].downloadUrl` (presigned S3) and `hlsUrl`; the response also sets `CloudFront-Policy`, `CloudFront-Signature`, `CloudFront-Key-Pair-Id` cookies |
| Video | `GET {chunks[].downloadUrl}` for each chunk | Separate MP4 files in playback order, joined with ffmpeg's concat demuxer (no re-encode). Presigned and short-lived: no auth or cookies, query string exactly as signed. A clip with no chunks yet is retried next sync |
| Thumbnail | `GET {videoThumbnail}` | Needs the CloudFront cookies from the clip detail, otherwise 403 |
| Events | `GET .../cameras/{deviceId}/events` | AI detections; useful for matching clips to cats |
| Activity log | `GET https://ub.prod.iothings.site/robots/{serial}/activities?limit=&offset=` | `PET_VISIT` rows carry `petWeight` (lb × 100), `duration`, `wasteType`, `petIds`, `cameraEventIds` |
| Pets | GraphQL `https://pet-profile.iothings.site/graphql/` | Profiles and `weightHistory` |
| Live view | `GET https://watford.ienso-dev.com/api/device-manager/client/generate-session/{deviceId}` then WebRTC signaling over `wss://.../api/signaling` | The response carries TURN credentials; the browser is the WebRTC peer and the server relays the offer, answer and ICE candidates (pylitterbot's `CameraSignalingRelay`) |

## Unverified (check with `--dump` on first run)

- Whether `/videos/{date}` buckets by UTC or local date. The script queries a day
  either side of the window and dedupes by clip id, so either way is covered.
- Whether the list is paginated beyond what's returned. If `clips.json` stops at a
  suspiciously round number, look for a cursor field in the raw response.
- Activity log: how far back `/activities` goes, whether `offset` paging works (the
  collector stops if a page repeats), and the exact `wasteType` labels. `--dump`
  now also writes `activities.json` and `pets.json` so you can check.
- Clip ↔ visit matching is by time (a visit is logged when the cat leaves, and its
  clip starts earlier). Tune `MATCH_BEFORE_S` / `MATCH_AFTER_S` if clips line up
  with the wrong visit. Clips up to 30 minutes away still show as "nearby".
- Retention: without Whisker+, the app shows 7 days of history, so run this at least
  weekly or clips age out.

## If you want to dig further

The app pins the certificate for `watford.ienso-dev.com`, so a plain mitmproxy
setup won't see the live-view signaling. The `execute-api` hosts were reverse-engineered
from decompiled app source rather than intercepted traffic. For anything new, the
quickest route is the Android APK (jadx) and searching for `execute-api` and
`camera-videos`, or Frida with a generic unpinning script on a rooted Android device.
