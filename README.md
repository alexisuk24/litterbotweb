# Litter-Robot 5 Pro clip downloader

Pulls recorded camera clips from the Whisker cloud for every Litter-Robot 5 Pro on
your account and saves them as MP4 (plus a thumbnail and the raw clip JSON).

It also collects the robot's metadata into a local SQLite database
(`/data/litterrobot.db`): pets and their weight history, the activity log (each pet
visit with weight, duration and waste type, plus clean cycles and offline events),
robot snapshots (litter level, drawer level), and an index of every clip.

## Quick start (Docker)

The image is on Docker Hub as
[`alexisuk24/litterbotweb`](https://hub.docker.com/r/alexisuk24/litterbotweb).
In a folder with `clips` and `data` folders owned by uid/gid 1000
(`sudo chown -R 1000:1000 clips data`):

```yaml
# compose.yml
services:
  lr-clips:
    image: alexisuk24/litterbotweb:latest
    container_name: lr-clips
    restart: unless-stopped
    environment:
      LR_USERNAME: ${LR_USERNAME:?Set LR_USERNAME in .env}
      LR_PASSWORD: ${LR_PASSWORD:?Set LR_PASSWORD in .env}
      TZ: ${TZ:-UTC}
    ports:
      - "2287:2287"
    volumes:
      - ./clips:/clips
      - ./data:/data
```

Put `LR_USERNAME`, `LR_PASSWORD` and `TZ` in a `.env` file next to it, run
`docker compose up -d`, and open `http://<host>:2287`. The
[container guide](docs/docker.md), which is also the Docker Hub overview, covers tags,
volumes, settings, hardware encoding, monitoring and updating. This repo's
[`compose.yml`](compose.yml) and [`.env.example`](.env.example) have every option.

## Web UI (port 2287)

- **Events**: every pet visit with the camera clips recorded around it. Click a
  visit to watch its clip(s), see the weight/duration, and fix which cat it was.
  Visits whose weight looks like a different cat (or that have no cat) are flagged
  **Check**; the *Needs review* filter lists just those. Keyboard: `↑/↓` move,
  `1–9` assign cat, `0` unknown, `u` undo.
  Visits are marked **Ignored** when they weigh under 2.6 lb (too light to be a cat; any
  cat Whisker assigned is removed there too, for visits from the last 7 days) or have no
  cat and no camera clip. Ignored visits are left out of the Cats page, warnings, baselines
  and vet reports; the *Ignored* filter lists them. Visits under 2.6 lb can't be given a
  cat; for the others, assigning a cat yourself counts the visit again.
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
- **Cats** (formerly Analysis): insights (visit-frequency changes, weight changes of 5% or more,
  cats that haven't visited, offline events), per-cat cards, visits per day, weight
  trend, time-of-day and weekday×hour patterns, clean cycles, litter/drawer levels,
  and a table of the robot's most common cat mix-ups based on your corrections.
  Each cat also gets its own normal range, learned from the 30 days before the last 7:
  visits per day, visit length and weight. The cat cards show it, and insights flag when
  the last 7 days fall outside it, when a cat visits far more often than usual in 24 hours,
  and when it has several unusually long visits (noting those with no waste detected,
  which can mean straining).
  **Report** on a cat card downloads a PDF for your vet over a date range you pick: a
  summary against the cat's usual ranges, the weight trend, visits per day split by urine
  and stool, anything unusual (long visits, bursts of visits, long gaps, stretches without
  stool, weight outside the usual range), a day-by-day table and your notes on visits.

The Cats page uses your corrections.

**Settings** also shows the app logs: the sync collector's log (downloads, activity
sync, Whisker updates), the web server's warnings and errors, and the access log (who
opened the app, and refused requests; see Access control), with a
warnings-and-errors filter and a download button. The sync log has the same lines `docker logs`
shows, but kept across container restarts.

**Robots** has a page per robot: status, faults, waste drawer and litter levels,
cycle counts, filter date, Wi-Fi and firmware, plus actions to start a clean cycle,
reset the waste drawer gauge after emptying it, and remote-reset the robot (each asks
for confirmation). The waste drawer tile counts clean cycles since the drawer was last seen emptied (a drop in its level between syncs), since neither Whisker nor the robot reports that. **Total cycles** is the robot's clean-cycle count for all time: resetting
the counter in the Whisker app starts a new one here and adds it on, rather than losing the
earlier cycles (counted from when this version of the app first ran). Firmware updates aren't possible from here; Whisker's LR5 API has no
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

**Notifications** (Settings): push notifications through [Pushover](https://pushover.net).
Enter your Pushover user key and an API token (create an application on pushover.net),
optionally the address you open this app at so notifications link back to it, and use
**Send test** to check. After each sync the collector sends anything new: each cat's
warnings from the Cats page and robot problems (drawer full, other faults, litter low,
offline). Each type of alert has its own checkbox, all on by default except **Every visit**,
which sends each cat's visit (the cat Whisker matched, its weight and the waste type) within
a minute or two of the clean cycle finishing.

**Robot IQ** forecasts when each robot's waste drawer will be full and its litter hopper
will need refilling, shown on the robot's page and sent as notifications a chosen number
of days ahead (Settings). The drawer forecast uses how much the fill level rises per clean
cycle since it was last emptied and the robot's recent cycles per day, up to the level at
which that robot reports the drawer full. The LR5 reports no hopper level, only Ready /
Litter low / Empty, so the hopper forecast starts once a refill has been seen running down
to Litter low: the dispenses that took set the capacity. Until then you're notified when
the robot reports the hopper low or empty.
A problem is sent once and again only if it clears and comes back; several new ones arrive
as one notification. A burst of visits in 24 hours is sent at high priority.

Between syncs, the collector checks each robot's status every minute (one small request per
robot, as Home Assistant's Litter-Robot integration does). When a clean cycle finishes it fetches
that robot's activity log straight away, then looks again 1, 5 and 15 minutes later for its
newest clips, camera detections and anything else Whisker lists late. So visits and their clips
appear within a minute or two of the cycle finishing, whatever the sync schedule. While a cat
is detected and the robot waits to cycle, it also checks the activity log at each status check,
and the sync log records when each new entry was listed (to learn whether visits can be
fetched sooner). The scheduled sync still catches anything this misses. Set
`LR_STATUS_INTERVAL=0` to turn it off.

**Refresh** (top right on every page) runs a sync immediately instead of waiting for
the next scheduled one. It's disabled while a sync is running and for 5 seconds after
one finishes.

**Retention** (Settings > Clips) works in three steps. Clips stay **full quality** as
downloaded for a number of days (14 by default, from `LR_RETENTION_DAYS`). After that
they can be kept as a **small copy**, re-encoded at 360p (about 5% of the size), up to an
age you choose. Past that (or straight away, with no small copy) the **video is removed**
and only a picture of the event is kept: Whisker's thumbnail, or a frame saved from the
clip when there isn't one. Clips marked **📌 Keep forever** (button under the player on the
Events and Clips pages) are never shrunk or removed. Making either step sooner waits
`LR_RETENTION_GRACE_HOURS` (24) before changing anything, with a banner, so you can mark
clips first. Shrinking is limited to 10 minutes per sync; a backlog carries over. Clips
whose video was removed are never re-downloaded; their visit history stays.

**Health check**: the image has a Docker `HEALTHCHECK` that calls `/api/health` every
minute. The container shows as *unhealthy* in `docker ps` when the web UI stops answering,
the database can't be opened, a sync has been running for over 3 hours, or no sync has
finished in three times the sync interval. A failing sync (e.g. Whisker being down) doesn't
count, since restarting wouldn't help; `docker inspect` shows its last error. Docker only
reports the status; to restart an unhealthy container automatically, use something like
[autoheal](https://github.com/willfarrell/docker-autoheal).

### Access control (Cloudflare Access)

Off by default: anyone who can open the app can change everything. To give people roles,
put the app behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/)
and set these in `compose.yml`:

| Variable | Example | |
| --- | --- | --- |
| `LR_AUTH_HEADER` | `Cf-Access-Authenticated-User-Email` | Header that names the user; turns roles on |
| `LR_AUTH_TEAM_DOMAIN` | `yourteam.cloudflareaccess.com` | With `LR_AUTH_AUD`, verify Cloudflare's signed `Cf-Access-Jwt-Assertion` |
| `LR_AUTH_AUD` | `4714c1358e65fe4b…` | The Access application's *Application Audience (AUD) Tag* |
| `LR_ADMINS` | `you@example.com,partner@example.com` | Admins; everyone else is a Viewer |

- **Admins** can do everything.
- **Viewers** can see every page except Settings, start a sync and download vet reports.
  They can't correct cats, use robot actions or the live view, keep or re-download clips,
  or see settings and logs. The server refuses these, not just the page.
- **Verifying the token** (set both `LR_AUTH_TEAM_DOMAIN` and `LR_AUTH_AUD`) makes the email
  header impossible to forge: each request's signed token is checked against your team's
  public keys and must name the same email. Without them the header is trusted as sent, so
  only skip it if port 2287 can't be reached except through Cloudflare.
- **Requests without an identity** have admin access when they don't come through Cloudflare
  (direct access on your LAN), and are refused when they do (a hostname or path without
  an Access policy). So anyone who can reach port 2287 directly is an admin; firewall it if
  that isn't what you want. `/api/health` needs no identity, for Docker and Uptime Kuma.
- These are environment variables on purpose: nothing in the web UI can change them. The
  Settings page shows the current setup.
- **Access log** (Settings > Logs > Access, `/data/logs/access.log`): one line when someone
  starts a session, with their email or address, role, and country, and every refused
  request, at most once every 10 minutes for the same person and page. A session is a
  Cloudflare Access sign-in when the token is verified, otherwise the first request after
  30 minutes idle. Live views are logged when they start and end, with the robot, how long
  they ran and why they ended (stopped, the 10-minute limit, page hidden or closed, connection
  lost, or the page stopped responding). Health checks (`/api/health`) aren't logged anywhere.

### Monitoring with Uptime Kuma

Add an **HTTP(s)** monitor pointed at the same `/api/health` endpoint. It answers 200 when
everything's fine and 503 when something's wrong, so Kuma's default accepted status codes
(200-299) already mark it down at the right times.

| Field | Value |
| --- | --- |
| Monitor Type | HTTP(s) |
| Friendly Name | Litter Robot |
| URL | `http://<host>:2287/api/health` |
| Heartbeat Interval | 60 seconds |
| Retries | 2 |
| Accepted Status Codes | 200-299 (the default) |

Use the address Kuma can reach directly (for example the server's LAN address), not one
behind a login page such as Cloudflare Access, or Kuma will be checking the login page.
When the monitor goes down, open the URL to see why, for example:

```json
{"ok": false, "problem": "no sync has run for 5.0 hours (schedule: every 60 min)",
 "last_sync": 1795232400.0, "last_error": null, "sync_running": false}
```

A sync that fails (for example because Whisker is down) keeps the monitor up, since nothing
on this side can fix it; `last_error` shows the message. For alerts, Kuma's own
notifications (including Pushover) work alongside the ones this app sends.

If you run [AutoKuma](https://github.com/BigBoot/AutoKuma), which creates Kuma monitors from
Docker labels, add these to the service in `compose.yml` instead, then set the interval and
retries in Kuma:

```yaml
    labels:
      kuma.litter-robot.http.name: "Litter Robot"
      kuma.litter-robot.http.url: "http://<host>:2287/api/health"
```

Built on [pylitterbot](https://github.com/natekspencer/pylitterbot) 2025.9.0+, which
already reverse-engineered the LR5 Pro camera APIs.

## Run it

```bash
pip install "pylitterbot>=2025.9.0" flask aiohttp reportlab   # ffmpeg must be on PATH
export LR_USERNAME=you@example.com LR_PASSWORD=...
python lr_clips.py --dump ./api-dump    # first: look at what the API returns
python lr_clips.py --out ./clips        # then: download the last 7 days
```

Or as a container that syncs hourly (see [Quick start](#quick-start-docker)): copy
`compose.yml` and `.env.example` to a folder, rename `.env.example` to `.env` and fill it
in, then run `docker compose pull && docker compose up -d`. Already-downloaded clips are skipped.
`compose.yml` uses the published image; to run your own changes, build it first under the
same name (`docker build -t alexisuk24/litterbotweb:latest .`), then `docker compose up -d`.

Output layout: `clips/<robot name>/<robot name>_<YYYY-MM-DD>_<HH-MM-SS>.mp4`

Volumes: `/clips` (videos) and `/data` (SQLite database, plus each cat's profile photo in
`/data/pet-photos`, copied at sync because Whisker's photo links expire within hours,
and log files in `/data/logs`).
The container runs as uid/gid 1000, so both host folders must be owned by it
(`sudo chown -R 1000:1000 <folder>`); it exits with that hint if they aren't.

Releases: feature branches are merged into `develop` by PR, and each merge publishes a
build of the next release (`0.3.0-dev.52`, plus a moving `develop` tag) to Docker Hub.
To release, open a PR from `develop` to `main` and merge it with a merge commit (not
squash, or the two branches' histories split). That publishes `0.3.0`, `0.3` and `latest`,
tags the commit `v0.3.0`, and deletes the `-dev.N` tags of `0.3.0` and earlier versions
from Docker Hub (the `develop` tag stays). The version comes from `VERSION`, which holds only
major.minor: raise it (in the release PR or earlier on `develop`) for a minor or major
release, or leave it for a patch release (`0.3.1`, `0.3.2`, ...). Hotfixes go from a branch
off `main` straight to `main`; merge `main` back into `develop` afterwards. The workflow
needs the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repo secrets; the running version is
shown at the top of the Settings page and is in the image's `APP_VERSION` env var and
`org.opencontainers.image.version` label.

Publishing the image by hand (with the SBOM and provenance attestations Docker Scout
checks for). `latest` includes the VAAPI drivers for both Intel (`intel`) and AMD (`mesa`) GPUs:

```bash
docker buildx build --pull --platform linux/amd64 \
  --provenance=mode=max --sbom=true \
  -t alexisuk24/litterbotweb:latest --push .
```

For a smaller image, keep only the driver your GPU needs with `--build-arg VA_DRIVERS=intel`
(amd64 only) or `VA_DRIVERS=mesa` (AMD), or use `VA_DRIVERS=""` for no hardware encoding
(clips that switch cameras are re-encoded on the CPU). The default is `"intel mesa"`.

| Env var | Default | Purpose |
|---|---|---|
| `LR_DOWNLOAD_INTERVAL` | 3600 | Seconds between sync runs (clips and metadata), until a schedule is picked under Settings > Sync schedule |
| `LR_STATUS_INTERVAL` | 60 | Seconds between robot status checks between syncs, used to fetch a visit soon after its clean cycle (minimum 30, `0` = off) |
| `LR_ACTIVITY_MAX_PAGES` | 50 | Pages of 100 activities to backfill on the first run |
| `LR_HWACCEL` | auto | Encoder for clips that switch cameras mid-clip (front at 1080p, then globe at 720p), which are re-encoded at 1080p so they don't freeze: `auto` uses VAAPI when `/dev/dri` is mapped in (see `compose.yml`) and the CPU otherwise, `vaapi` warns if no device is found, `off` always uses the CPU |
| `MATCH_BEFORE_S` / `MATCH_AFTER_S` | 600 / 180 | How far before/after a visit a clip still counts as that visit's clip |
| `LR_RETENTION_DAYS` | 14 | Delete clips older than this unless kept (0 = keep everything), until a choice is made under Settings > Clips |
| `LR_RETENTION_GRACE_HOURS` | 24 | Delay before the first deletions after retention is enabled |
| `LR_WEB` | 1 | Serve the web UI on port 2287 from the collector's process (`0` = collector only; run `python web.py` separately) |
| `DB_PATH` | /data/litterrobot.db | Database location |
| `LOG_DIR` | /data/logs | Where `sync.log`, `web.log` and `access.log` are written (1 MB each, 3 rotated copies kept) |

## Tests

```bash
pip install "pylitterbot>=2025.9.0" flask aiohttp reportlab "pyjwt[crypto]" pytest
python -m pytest
```

Each test gets its own empty database and made-up robots, cats and visits; nothing touches
Whisker or your data. The retention tests that encode video need `ffmpeg` and are skipped
without it. GitHub Actions runs the tests on every push, and also checks that the browser
scripts parse.

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
