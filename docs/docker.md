# Litter-Robot 5 Pro clip downloader

A self-hosted container for Litter-Robot 5 Pro owners. It downloads the camera clips from
your Whisker account before Whisker deletes them (after about 7 days without Whisker+),
keeps a history of every visit, and serves a web UI on port 2287 to browse them.

- **Events:** every pet visit with its clips, weight and duration. Fix which cat it was,
  and the correction is sent back to Whisker too.
- **Cats:** each cat's visits, weight trend and usual ranges, alerts when something
  changes, and a PDF report for your vet.
- **Robots:** status, drawer and litter levels, forecasts of when the drawer will be full
  and the litter will run out, clean cycle and reset actions, and the live camera view.
- **Notifications** through Pushover, **retention** that shrinks or removes old clips
  (except those you keep), optional **Admin/Viewer roles** with Cloudflare Access, and a
  Docker health check.

Source, full documentation and issues: https://github.com/alexisuk24/litterbotweb

## Quick start

1. Create a folder with `clips` and `data` folders in it, owned by uid/gid 1000 (the
   container's user):

   ```bash
   mkdir -p litter-robot/clips litter-robot/data && cd litter-robot
   sudo chown -R 1000:1000 clips data
   ```

2. Save this as `compose.yml`:

   ```yaml
   services:
     lr-clips:
       image: alexisuk24/lr_video_downloader:latest
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

3. Save your Whisker login as `.env` next to it:

   ```bash
   LR_USERNAME=you@example.com
   LR_PASSWORD=your-whisker-password
   TZ=Europe/London
   ```

4. Start it, and open `http://<host>:2287`:

   ```bash
   docker compose up -d
   docker compose logs -f   # watch the first sync
   ```

The first sync downloads the clips Whisker still has and backfills the visit history.
After that it syncs every hour, or on the schedule you pick under Settings. The clips
already downloaded are skipped.

The same setup with `docker run`:

```bash
docker run -d --name lr-clips --restart unless-stopped -p 2287:2287 \
  -e LR_USERNAME=you@example.com -e LR_PASSWORD=your-whisker-password -e TZ=Europe/London \
  -v "$PWD/clips:/clips" -v "$PWD/data:/data" \
  alexisuk24/lr_video_downloader:latest
```

## Tags

| Tag | What it is |
|---|---|
| `latest` | The latest release. Use this one. |
| `0.2.0`, `0.2` | A specific release, or the latest patch release of a minor version |
| `develop` | The latest development build: newest changes, less tested |
| `0.2.1-dev.14` | A specific development build |

Images are built for `linux/amd64` and come with SBOM and provenance attestations.

## Volumes and ports

| Path | Contents |
|---|---|
| `/clips` | The videos: `<robot name>/<robot name>_<YYYY-MM-DD>_<HH-MM-SS>.mp4`, with a thumbnail and the clip's JSON |
| `/data` | The SQLite database, cat photos (`pet-photos/`) and logs (`logs/`) |
| Port `2287` | The web UI and `/api/health` |

Both folders must be owned by uid/gid 1000. The container exits with a hint if they aren't.

## Settings

Most settings are in the web UI, including the sync schedule, retention, weight unit,
notifications and Whisker write-back. These environment variables are the ones you're
most likely to set:

| Variable | Default | Purpose |
|---|---|---|
| `LR_USERNAME` / `LR_PASSWORD` | (required) | Your Whisker account |
| `TZ` | `UTC` | The container's timezone, for logs and the sync schedule |
| `LR_DOWNLOAD_INTERVAL` | `3600` | Seconds between syncs, until a schedule is picked in Settings |
| `LR_RETENTION_DAYS` | `14` | Days to keep clips at full quality, until set in Settings (`0` keeps everything) |
| `LR_HWACCEL` | `auto` | Encoder for clips that switch cameras: `auto`, `vaapi` or `off` (CPU) |
| `LR_WEB` | `1` | `0` runs the collector without the web UI |

The README lists [every variable](https://github.com/alexisuk24/litterbotweb#run-it).

## Hardware video encoding (optional)

Clips that switch cameras mid-clip are re-encoded. To use an Intel or AMD GPU (VAAPI)
instead of the CPU, add this to the service, with the host's `render` group id from
`getent group render | cut -d: -f3`:

```yaml
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "109"
```

## Access control (optional)

By default, anyone who can open the app can change everything. To give people Admin or
Viewer roles, put the app behind Cloudflare Access and set `LR_AUTH_HEADER`,
`LR_AUTH_TEAM_DOMAIN`, `LR_AUTH_AUD` and `LR_ADMINS`. The README explains
[the setup and what each role can do](https://github.com/alexisuk24/litterbotweb#access-control-cloudflare-access).

## Health and monitoring

The image has a Docker `HEALTHCHECK`. `docker ps` shows the container as *unhealthy* when
the web UI stops answering, the database can't be opened, or syncs have stopped.
`http://<host>:2287/api/health` returns 200 or 503 with the reason, and works as an
[Uptime Kuma monitor](https://github.com/alexisuk24/litterbotweb#monitoring-with-uptime-kuma).

## Updating

```bash
docker compose pull && docker compose up -d
```

The database is upgraded automatically when the new version starts, and your clips and
settings are kept.
