# Litter-Robot 5 Pro clip downloader

Pulls recorded camera clips from the Whisker cloud for every Litter-Robot 5 Pro on
your account and saves them as MP4 (plus a thumbnail and the raw clip JSON).

Built on [pylitterbot](https://github.com/natekspencer/pylitterbot) 2025.6.5+, which
already reverse-engineered the LR5 Pro camera APIs.

## Run it

```bash
pip install "pylitterbot>=2025.6.5"     # ffmpeg must be on PATH
export LR_USERNAME=you@example.com LR_PASSWORD=...
python lr_clips.py --dump ./api-dump    # first: look at what the API returns
python lr_clips.py --out ./clips        # then: download the last 7 days
```

Or as a container that polls hourly: copy this folder to
`/data/Containers/home/litterrobot-clips/`, create `.env` from `.env.example`, then
`docker compose up -d --build`. Already-downloaded clips are skipped.

Output layout: `clips/<robot name>/<YYYY-MM-DD>/<HHMMSS>_<eventType>_<id>.mp4`

## How the API works

| Step | Request | Notes |
|---|---|---|
| Login | AWS Cognito via the Whisker app client | pylitterbot handles token refresh |
| Robots | `GET https://ub.prod.iothings.site/robots` | Pro units have `type: LR5_PRO` and `cameraMetadata.deviceId` |
| List clips | `GET https://rrntg65uwf.execute-api.us-east-1.amazonaws.com/prod/v1/cameras/{deviceId}/videos[/{YYYY-MM-DD}]` | Bearer token **and** `x-api-key` (the app's static key, bundled in pylitterbot) |
| Clip detail | `GET .../prod/v1/camera-videos/{clipId}` | Response sets `CloudFront-Policy`, `CloudFront-Signature`, `CloudFront-Key-Pair-Id` cookies; body has `hlsUrl` |
| Video | `GET {hlsUrl}` and each segment | Needs those cookies on every request, otherwise 403. Cookies are short-lived, so fetch them right before downloading |
| Events | `GET .../cameras/{deviceId}/events` | AI detections; useful for matching clips to cats |
| Live view | `GET https://watford.ienso-dev.com/api/device-manager/client/generate-session/{deviceId}` then WebRTC signaling over `wss://.../api/signaling` | Not needed for clips |

## Unverified (check with `--dump` on first run)

- Whether `/videos/{date}` buckets by UTC or local date. The script queries a day
  either side of the window and dedupes by clip id, so either way is covered.
- Whether the list is paginated beyond what's returned. If `clips.json` stops at a
  suspiciously round number, look for a cursor field in the raw response.
- Retention: without Whisker+, the app shows 7 days of history, so run this at least
  weekly or clips age out.

## If you want to dig further

The app pins the certificate for `watford.ienso-dev.com`, so a plain mitmproxy
setup won't see the live-view signaling. The `execute-api` hosts were reverse-engineered
from decompiled app source rather than intercepted traffic. For anything new, the
quickest route is the Android APK (jadx) and searching for `execute-api` and
`camera-videos`, or Frida with a generic unpinning script on a rooted Android device.
