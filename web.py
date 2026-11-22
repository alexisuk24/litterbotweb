#!/usr/bin/env python3
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import math
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   send_file, url_for)
from markupsafe import escape

from pylitterbot.camera import local_timezone_name

import lr_auth
import lr_db
import lr_iq
import lr_live

app = Flask(__name__)
CLIPS = Path(os.environ.get("CLIPS_PATH", "/clips"))
CHART_JS = Path(__file__).parent / "static" / "vendor" / "chart.umd.min.js"
CHART_JS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"
# A visit's timestamp is when the robot logged it (end of visit); its clip
# usually starts a little before. These bound which clips count as "this visit".
MATCH_BEFORE_S = int(os.environ.get("MATCH_BEFORE_S", "600"))
MATCH_AFTER_S = int(os.environ.get("MATCH_AFTER_S", "180"))
NEARBY_S = 1800
REFRESH_COOLDOWN_S = 5
STALE_RUN_S = 3 * 3600  # a "running" flag older than this means the collector died mid-run
_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_")
_TIME_RE = re.compile(r"_(\d{2}-\d{2}-\d{2})\.mp4$")


def db():
    if "db" not in g:
        g.db = lr_db.connect()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def local_tz() -> ZoneInfo:
    row = db().execute("SELECT timezone FROM robots WHERE timezone IS NOT NULL LIMIT 1").fetchone()
    for name in (row and row["timezone"], os.environ.get("TZ"), "UTC"):
        try:
            if name:
                return ZoneInfo(name)
        except Exception:
            continue
    return ZoneInfo("UTC")


# ------------------------------------------------------------------ pages


def weight_unit() -> str:
    return "lb" if lr_db.get_state(db(), "weight_unit") == "lb" else "kg"


def fmt_weight(lb: float, unit: str, signed: bool = False) -> str:
    value = lb * 0.453592 if unit == "kg" else lb
    return f"{value:+.2f} {unit}" if signed else f"{value:.2f} {unit}"


@app.context_processor
def _inject_unit():
    return {"weight_unit": weight_unit(), "user": g.get("user") or lr_auth.User(None, "admin")}


@app.before_request
def _check_access():
    """Who's asking (lr_auth) and whether they may: Viewers can look but not change anything."""
    if request.path == "/api/health":  # Docker and uptime monitors carry no identity
        return None
    try:
        g.user = lr_auth.identify(request.headers)
    except lr_auth.Denied as d:
        lr_auth.log_refusal(None, d.status, d.message, request.method, request.path,
                            request.headers, request.remote_addr)
        return _refuse(d.status, d.message)
    if lr_auth.HEADER and g.user.email is None:  # admin only because it bypassed Cloudflare
        lr_auth.note_direct(request.remote_addr)
    lr_auth.log_access(g.user, request.headers, request.remote_addr)
    if not lr_auth.allowed(g.user, request.method, request.path):
        message = "Your account can view this app but not change anything. Ask an admin."
        lr_auth.log_refusal(g.user, 403, "Viewers can't do this", request.method, request.path,
                            request.headers, request.remote_addr)
        return _refuse(403, message)
    return None


def _refuse(status: int, message: str):
    if request.path.startswith("/api/") or request.method != "GET":
        return jsonify(error=message), status
    return Response(f"<!doctype html><meta name=viewport content='width=device-width'>"
                    f"<body style='font-family:system-ui;background:#0f1117;color:#e2e8f0;padding:2rem'>"
                    f"<h1 style='font-size:1.2rem'>Litter Robot</h1><p>{escape(message)}</p>"
                    f"<p><a href='/robots' style='color:#b3a8ff'>Back to the app</a></p>",
                    status=status, mimetype="text/html")


@app.after_request
def _no_cache_pages(resp):
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


_asset_versions: dict[str, str] = {}


@app.template_global()
def asset(path: str) -> str:
    """Static URL with a content hash, so a new release is never served from a stale
    cache (browser, reverse proxy or CDN) under the old URL."""
    if path not in _asset_versions:
        f = Path(app.static_folder) / path
        _asset_versions[path] = hashlib.sha1(f.read_bytes()).hexdigest()[:10] if f.is_file() else ""
    v = _asset_versions[path]
    return f"/static/{path}?v={v}" if v else f"/static/{path}"


@app.route("/")
def index():
    if request.args.get("robot"):  # old share links pointed at the clips page on /
        return redirect(url_for("clips_page", **request.args))
    return redirect(url_for("robots_page"))


@app.route("/clips")
def clips_page():
    return render_template("clips.html", page="clips")


@app.route("/events")
def events_page():
    return render_template("events.html", page="events")


@app.route("/cats")
def cats_page():
    return render_template("analysis.html", page="cats")


@app.route("/analysis")
def analysis_page():  # the Cats page's old address
    return redirect(url_for("cats_page", **request.args))


@app.route("/settings")
def settings_page():
    return render_template("settings.html", page="settings")


@app.route("/robots")
def robots_page():
    first = db().execute("SELECT serial FROM robots ORDER BY name LIMIT 1").fetchone()
    if first:
        return redirect(url_for("robot_page", serial=first["serial"]))
    return render_template("robot.html", page="robots", serial="")


@app.route("/robots/<serial>")
def robot_page(serial):
    if not db().execute("SELECT 1 FROM robots WHERE serial = ?", (serial,)).fetchone():
        abort(404)
    return render_template("robot.html", page="robots", serial=serial)


# ------------------------------------------------------------ robot API


def _command_json(c):
    return {"id": c["id"], "command": c["command"], "status": c["status"], "error": c["error"],
            "created": c["created_at"], "updated": c["updated_at"]}


@app.route("/api/robot/<serial>")
def api_robot(serial):
    conn = db()
    r = conn.execute("SELECT * FROM robots WHERE serial = ?", (serial,)).fetchone()
    if not r:
        abort(404)
    since = time.time() - 86400
    counts = {row["type"]: row["n"] for row in conn.execute(
        "SELECT type, COUNT(*) AS n FROM activities WHERE robot_serial = ? AND ts >= ? GROUP BY type", (serial, since))}
    last_visit = conn.execute("SELECT MAX(ts) AS ts FROM activities WHERE robot_serial = ? AND type = 'PET_VISIT'",
                              (serial,)).fetchone()["ts"]
    recent = [dict(id=a["id"], ts=a["ts"], type=a["type"], subtype=a["subtype"]) for a in conn.execute(
        "SELECT id, ts, type, subtype FROM activities WHERE robot_serial = ? AND type != 'PET_VISIT' "
        "ORDER BY ts DESC LIMIT 12", (serial,))]
    commands = [_command_json(c) for c in conn.execute(
        "SELECT * FROM robot_commands WHERE robot_serial = ? ORDER BY id DESC LIMIT 20", (serial,))]
    latest = {}
    for c in commands:
        latest.setdefault(c["command"], c)
    return jsonify(
        robot={"serial": r["serial"], "name": r["name"], "model": r["model"], "timezone": r["timezone"],
               "camera": bool(r["camera_device_id"])},
        robots=[dict(x) for x in conn.execute("SELECT serial, name FROM robots ORDER BY name")],
        info=json.loads(r["info"]) if r["info"] else None, info_at=r["info_at"],
        clips_stale=json.loads(lr_db.get_state(conn, f"clips_stale:{serial}") or "null"),
        last_24h={"visits": counts.get("PET_VISIT", 0), "cycles": counts.get("CYCLE_COMPLETED", 0)},
        last_visit=last_visit, recent=recent, latest=latest, iq=lr_iq.forecasts(conn, serial),
        history=[c for c in commands if c["command"] != "refresh"][:6],
        firmware=[{"component": f["component"], "version": f["version"], "previous": f["previous"],
                   "at": f["seen_at"]} for f in conn.execute(
            "SELECT * FROM firmware_history WHERE robot_serial = ? ORDER BY seen_at DESC, id DESC", (serial,))],
    )


@app.route("/api/robot/<serial>/command", methods=["POST"])
def api_robot_command(serial):
    conn = db()
    if not conn.execute("SELECT 1 FROM robots WHERE serial = ?", (serial,)).fetchone():
        abort(404)
    command = (request.get_json(force=True, silent=True) or {}).get("command")
    if command not in lr_db.ROBOT_COMMANDS:
        abort(400)
    conn.execute("BEGIN IMMEDIATE")
    busy = lr_db.active_command(conn, serial, command)
    if busy:
        conn.rollback()
        return jsonify(reason="in_progress", command=_command_json(busy)), 409
    cid = lr_db.enqueue_command(conn, serial, command)
    conn.commit()
    lr_db.wake()
    return jsonify(command=_command_json(conn.execute("SELECT * FROM robot_commands WHERE id = ?", (cid,)).fetchone())), 202


# ------------------------------------------------------------ live view


def _live(coro):
    """Run a live-view call, turning failures into a JSON error for the page."""
    try:
        return lr_live.run(coro)
    except lr_live.LiveError as err:
        abort(Response(json.dumps({"error": str(err)}), status=409, mimetype="application/json"))
    except Exception:
        logging.getLogger("lr_live").exception("Live view request failed")
        abort(Response(json.dumps({"error": "Whisker didn't respond; try again"}), status=502,
                       mimetype="application/json"))


@app.route("/api/live/<serial>", methods=["POST"])
def api_live_start(serial):
    if not db().execute("SELECT 1 FROM robots WHERE serial = ? AND camera_device_id IS NOT NULL",
                        (serial,)).fetchone():
        abort(404)
    return jsonify(_live(lr_live.start(serial, lr_auth.who(g.user, request.headers, request.remote_addr))))


@app.route("/api/live/<serial>/view", methods=["POST"])
def api_live_view(serial):
    view = (request.get_json(force=True, silent=True) or {}).get("view")
    if view not in ("front", "globe"):
        abort(400)
    return jsonify(ok=_live(lr_live.set_view(serial, view)))


@app.route("/api/live/session/<sid>/offer", methods=["POST"])
def api_live_offer(sid):
    sdp = (request.get_json(force=True, silent=True) or {}).get("sdp")
    if not isinstance(sdp, str) or not sdp:
        abort(400)
    return jsonify(sdp=_live(lr_live.offer(sid, sdp)))


@app.route("/api/live/session/<sid>/candidates", methods=["GET", "POST"])
def api_live_candidates(sid):
    if request.method == "POST":
        candidate = request.get_json(force=True, silent=True)
        if not isinstance(candidate, dict):
            abort(400)
        _live(lr_live.add_candidate(sid, candidate))
        return jsonify(ok=True)
    return jsonify(candidates=_live(lr_live.candidates(sid, request.args.get("after", 0, type=int))))


@app.route("/api/live/session/<sid>/close", methods=["POST"])
def api_live_close(sid):
    _live(lr_live.close(sid, lr_live.END_REASONS.get(request.args.get("reason"), "closed")))
    return jsonify(ok=True)


@app.route("/manifest.webmanifest")
def manifest():
    icon = lambda f, size, purpose="any": {"src": asset(f"icons/{f}"), "sizes": size, "type": "image/png",
                                           "purpose": purpose}
    resp = jsonify({
        "name": "Litter Robot", "short_name": "Litter Robot", "id": "/", "start_url": "/robots", "scope": "/",
        "display": "standalone", "background_color": "#0f1117", "theme_color": "#1a1d27",
        "icons": [icon("icon-192.png", "192x192"), icon("icon-512.png", "512x512"),
                  icon("icon-maskable-512.png", "512x512", "maskable")],
    })
    resp.mimetype = "application/manifest+json"
    return resp


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return send_file(Path(app.static_folder) / "icons" / "apple-touch-icon.png", max_age=86400)


@app.route("/favicon.ico")
def favicon():
    return send_file(Path(app.static_folder) / "icons" / "favicon-32.png", mimetype="image/png", max_age=86400)


@app.route("/static/vendor/chart.umd.min.js")
def chart_js():
    if CHART_JS.exists():
        return send_file(CHART_JS, mimetype="text/javascript", max_age=86400)
    return redirect(CHART_JS_CDN)


# ------------------------------------------------------------ sync / refresh


def _fstate(key):
    try:
        return float(lr_db.get_state(db(), key) or 0)
    except ValueError:
        return 0.0


def retention_info():
    if "retention" not in g:  # asked once per clip on the clip lists
        days, small = lr_db.retention_tiers(db())
        enabled = _fstate("retention_enabled_at")
        grace = float(os.environ.get("LR_RETENTION_GRACE_HOURS", "24"))
        g.retention = {"days": days, "small": small,
                       "small_setting": lr_db.get_state(db(), "retention_small") or "none",
                       "starts_at": enabled + grace * 3600 if days and enabled else None,
                       "minimum": int(_fstate("retention_min_days"))}
    return g.retention


def sync_status():
    now = time.time()
    started, finished = _fstate("sync_started"), _fstate("sync_finished")
    requested = _fstate("refresh_requested")
    running = lr_db.get_state(db(), "sync_running") == "1" and now - started < STALE_RUN_S
    pending = requested > started
    cooldown = max(0.0, REFRESH_COOLDOWN_S - (now - finished)) if finished else 0.0
    return {
        "running": running, "pending": pending, "started": started or None, "finished": finished or None,
        "trigger": lr_db.get_state(db(), "sync_trigger"), "error": lr_db.get_state(db(), "sync_error") or None,
        "cooldown": round(cooldown, 2), "can_refresh": not running and not pending and cooldown == 0,
        "collector_seen": bool(started), "retention": retention_info(), "now": now,
    }


@app.route("/api/health")
def api_health():
    """For Docker's HEALTHCHECK: 200 when the web server answers, the database opens and the
    collector's sync loop isn't stuck; 503 with the reason otherwise. A sync that failed
    (e.g. Whisker is down) still counts as healthy, since a restart wouldn't fix it; its
    error is included for `docker inspect`."""
    now = time.time()
    try:
        conn = db()
        conn.execute("SELECT 1").fetchone()
    except Exception as err:
        return jsonify(ok=False, problem=f"database: {err}"), 503
    started, finished = _fstate("sync_started"), _fstate("sync_finished")
    running = lr_db.get_state(conn, "sync_running") == "1"
    interval = lr_db.sync_interval(conn, int(os.environ.get("LR_DOWNLOAD_INTERVAL", "3600")))
    problem = None
    if running and now - started > STALE_RUN_S:
        problem = f"a sync has been running for {(now - started) / 3600:.1f} hours"
    elif not running and finished and now - finished > 3 * interval + 600:
        problem = f"no sync has run for {(now - finished) / 3600:.1f} hours (schedule: every {interval // 60} min)"
    body = {"ok": problem is None, "problem": problem, "sync_running": running,
            "last_sync": finished or None, "last_error": lr_db.get_state(conn, "sync_error") or None}
    return jsonify(body), 200 if problem is None else 503


@app.route("/api/sync", methods=["GET", "POST"])
def api_sync():
    if request.method == "GET":
        return jsonify(sync_status())
    conn = db()
    conn.execute("BEGIN IMMEDIATE")  # serialize concurrent clicks
    st = sync_status()
    if not st["can_refresh"]:
        conn.rollback()
        reason = "running" if st["running"] else "pending" if st["pending"] else "cooldown"
        return jsonify(reason=reason, **st), 429 if reason == "cooldown" else 409
    lr_db.set_state(conn, "refresh_requested", str(time.time()))
    conn.commit()
    lr_db.wake()
    return jsonify(sync_status()), 202


# ------------------------------------------------------------ visit model


def load_pets():
    """Pets in a stable order (by creation) so each cat keeps the same color."""
    rows = db().execute(
        "SELECT pet_id, name, weight, last_weight_reading, is_active, photo_file, photo_key FROM pets "
        "ORDER BY COALESCE(created_at, ''), name"
    ).fetchall()
    return [
        {"id": r["pet_id"], "name": r["name"], "slot": i, "active": bool(r["is_active"]),
         "profile_weight": r["last_weight_reading"] or r["weight"],
         "image": _pet_photo_url(r)}
        for i, r in enumerate(rows)
    ]


def _pet_photo_url(r) -> str | None:
    if not r["photo_file"]:
        return None
    try:
        mtime = (lr_db.photo_dir() / r["photo_file"]).stat().st_mtime_ns
    except OSError:
        return None
    # the version param changes whenever the file is rewritten, so browsers can cache it for long
    v = hashlib.sha1(f"{r['photo_key']}:{mtime}".encode()).hexdigest()[:10]
    return url_for("pet_photo", pet_id=r["pet_id"], v=v)


@app.route("/pet-photo/<pet_id>")
def pet_photo(pet_id):
    row = db().execute("SELECT photo_file FROM pets WHERE pet_id = ?", (pet_id,)).fetchone()
    if not row or not row["photo_file"]:
        abort(404)
    root = lr_db.photo_dir().resolve()
    path = (root / row["photo_file"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        abort(404)
    return send_file(path, max_age=30 * 86400)


def load_visits(start: float | None = None, end: float | None = None, robot: str | None = None,
                types=("PET_VISIT",)):
    sql = ["SELECT a.*, o.pet_id AS o_pet, o.note AS o_note, "
           "o.activity_id IS NOT NULL AS has_override, r.name AS robot_name "
           "FROM activities a LEFT JOIN visit_overrides o ON o.activity_id = a.id "
           "LEFT JOIN robots r ON r.serial = a.robot_serial WHERE 1=1"]
    params: list = []
    if types:
        sql.append(f"AND a.type IN ({','.join('?' * len(types))})")
        params += list(types)
    if start is not None:
        sql.append("AND a.ts >= ?"); params.append(start)
    if end is not None:
        sql.append("AND a.ts < ?"); params.append(end)
    if robot:
        sql.append("AND a.robot_serial = ?"); params.append(robot)
    sql.append("ORDER BY a.ts DESC")
    out = []
    for r in db().execute(" ".join(sql), params):
        whisker_pet = lr_db.first_pet(r["pet_ids"])
        robot_pet = lr_db.first_pet(r["original_pet_ids"] or r["pet_ids"])
        pet = r["o_pet"] or whisker_pet
        pet = None if pet == lr_db.UNKNOWN_PET else pet
        manual_cat = r["o_pet"] not in (None, lr_db.UNKNOWN_PET)
        out.append({
            "id": r["id"], "ts": r["ts"], "type": r["type"], "subtype": r["subtype"],
            "robot": r["robot_serial"], "robot_name": r["robot_name"],
            "weight": r["pet_weight"], "duration": r["duration"],
            "waste_type": r["waste_type"], "waste_weight": r["waste_weight"],
            # api_pet is what the robot first recorded; whisker_pet is Whisker's current assignment
            "api_pet": robot_pet, "whisker_pet": whisker_pet, "pet": pet,
            "corrected": r["type"] == "PET_VISIT" and pet != robot_pet,
            "note": r["o_note"] or "",
            "camera_event_ids": json.loads(r["camera_event_ids"] or "[]"),
            "manual_cat": manual_cat,
        })
    mark_ignored(out)
    return out


IGNORE_UNDER_LB = 2.6  # lighter than any cat: a scale blip, or something left on the robot


def mark_ignored(visits) -> None:
    """Set each visit's `ignored` reason: 'light' (under IGNORE_UNDER_LB; any cat Whisker
    assigned is removed there, see queue_ignored_unassigns) or 'no_clip' (no cat and no clip
    recorded around it). Ignored visits show no cat and are left out of stats, baselines,
    warnings and reports. A light visit can't be given a cat (api_override refuses); a cat
    you assign yourself to a no-clip visit makes it count again."""
    pending = []
    for v in visits:
        v["ignored"] = None
        if v["type"] != "PET_VISIT":
            continue
        if v["weight"] is not None and v["weight"] < IGNORE_UNDER_LB:
            v["ignored"] = "light"
        elif not v["manual_cat"] and not v["pet"]:
            pending.append(v)
    with_clips = _visits_with_clips(pending)
    for v in pending:
        if v["id"] not in with_clips:
            v["ignored"] = "no_clip"
    for v in visits:
        if v["ignored"]:
            v["pet"], v["corrected"] = None, False


def queue_ignored_unassigns() -> int:
    """Ask the collector to remove the cat Whisker assigned to light (ignored) visits, within
    Whisker's edit window. Each visit is sent once; a failure isn't retried automatically."""
    conn = db()
    if not lr_db.writeback_enabled(conn):
        return 0
    n = 0
    for v in load_visits(time.time() - lr_db.WHISKER_EDIT_WINDOW_S, types=("PET_VISIT",)):
        if v["ignored"] == "light" and v["whisker_pet"] and not lr_db.latest_push(conn, v["id"]):
            lr_db.enqueue_push(conn, v["id"], None)
            n += 1
    conn.commit()
    if n:
        lr_db.wake()
    return n


def reference_weights(pets) -> dict[str, float]:
    """Each cat's typical weight: median of its (corrected) visit weights over the
    last 30 days, falling back to the Whisker profile weight."""
    since = time.time() - 30 * 86400
    by_pet = defaultdict(list)
    for v in load_visits(start=since):
        if v["pet"] and v["weight"] and not v["ignored"]:
            by_pet[v["pet"]].append(v["weight"])
    refs = {}
    for p in pets:
        if len(by_pet[p["id"]]) >= 3:
            refs[p["id"]] = statistics.median(by_pet[p["id"]])
        elif p["profile_weight"]:
            refs[p["id"]] = p["profile_weight"]
    return refs


def annotate(v, refs, active_ids):
    """Add a weight-based cat suggestion and a needs-review flag."""
    v["suggested"] = None
    v["review"] = False
    if v["type"] != "PET_VISIT" or v.get("ignored"):
        return v
    w = v["weight"]
    if w and refs:
        candidates = {pid: ref for pid, ref in refs.items() if pid in active_ids} or refs
        best = min(candidates, key=lambda pid: abs(candidates[pid] - w))
        v["suggested"] = best
        cur = refs.get(v["pet"]) if v["pet"] else None
        if cur is not None and best != v["pet"]:
            tol = max(0.5, 0.06 * cur)
            if abs(w - cur) > tol and abs(w - refs[best]) < abs(w - cur) - 0.3:
                v["review"] = True
    if not v["pet"]:
        v["review"] = True
    return v


def clip_rows(start: float, end: float):
    return db().execute(
        "SELECT c.*, r.serial AS r_serial FROM clips c LEFT JOIN robots r ON r.folder = c.robot_folder "
        "WHERE c.ts >= ? AND c.ts < ? ORDER BY c.ts", (start, end)
    ).fetchall()


def media_url(kind: str, folder: str, filename: str | None) -> str | None:
    """Video/thumbnail URL tagged with the file's mtime, so a re-downloaded clip is
    never served from a stale browser or CDN cache under the old URL."""
    if not filename:
        return None
    try:
        v = int((CLIPS / folder / filename).stat().st_mtime)
    except OSError:
        v = 0
    return f"/{kind}/{folder}/{filename}?v={v}"


def clip_json(c, event_ts=None, event_duration=None, camera_ids=(), downloads=None):
    folder = c["robot_folder"]
    offset = c["ts"] - event_ts if event_ts is not None else None
    in_window = offset is not None and -MATCH_BEFORE_S - (event_duration or 0) <= offset <= MATCH_AFTER_S
    return {
        "id": c["id"], "ts": c["ts"], "offset_s": offset, "event_type": c["event_type"],
        "duration_s": c["duration_s"], "pet_detections": json.loads(c["pet_detections"] or "[]"),
        "video": media_url("video", folder, c["video_file"]),
        "thumb": media_url("media", folder, c["thumb_file"]),
        "match": in_window or str(c["id"]) in {str(x) for x in camera_ids},
        "download": (downloads or {}).get(str(c["id"])),
        **keep_fields(c),
    }


def keep_fields(c):
    """Keep flag, quality, and what retention does to the clip next (`next`: 'small' when it
    becomes a small copy, 'picture' when its video goes) and when (`expires`)."""
    r = retention_info()
    days, small = r["days"], r["small"]
    expires = nxt = None
    if days and not c["keep"] and not c["deleted_at"]:
        if c["quality"] is None:
            expires = c["ts"] + days * 86400
            nxt = "small" if small is not None and (small == 0 or c["ts"] + small * 86400 > expires) else "picture"
        elif small:
            expires, nxt = c["ts"] + small * 86400, "picture"
    return {"keep": bool(c["keep"]), "deleted": bool(c["deleted_at"]), "small": c["quality"] == "small",
            "expires": expires, "next": nxt}


@app.route("/api/clips/<clip_id>/download", methods=["POST"])
def api_download_clip(clip_id):
    """Queue a fresh download of this clip from Whisker (replacing the local copy)."""
    conn = db()
    c = conn.execute("SELECT c.id, COALESCE(c.robot_serial, r.serial) AS serial FROM clips c "
                     "LEFT JOIN robots r ON r.folder = c.robot_folder WHERE c.id = ?", (clip_id,)).fetchone()
    if not c:
        abort(404)
    if not c["serial"]:
        return jsonify(reason="unknown_robot"), 400
    conn.execute("BEGIN IMMEDIATE")
    if lr_db.active_command(conn, c["serial"], "redownload", str(clip_id)):
        conn.rollback()
        return jsonify(reason="in_progress", download=lr_db.clip_download_status(conn, [clip_id]).get(str(clip_id))), 409
    lr_db.enqueue_command(conn, c["serial"], "redownload", str(clip_id))
    conn.commit()
    lr_db.wake()
    return jsonify(download=lr_db.clip_download_status(conn, [clip_id]).get(str(clip_id))), 202


@app.route("/api/clips/<clip_id>/keep", methods=["POST"])
def api_keep(clip_id):
    body = request.get_json(force=True, silent=True) or {}
    conn = db()
    cur = conn.execute("UPDATE clips SET keep = ? WHERE id = ?", (1 if body.get("keep") else 0, clip_id))
    if not cur.rowcount:
        abort(404)
    conn.commit()
    c = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return jsonify(id=c["id"], **keep_fields(c))


# camera detections this close to a visit count as that visit's
CAM_BEFORE_S, CAM_AFTER_S = 180, 120


def attach_camera(visits, detail=False):
    """Add the camera's own cat recognition: its best match among detections around
    each visit. Similarity is 1 - cosine distance (higher = closer match)."""
    pv = [v for v in visits if v["type"] == "PET_VISIT"]
    if not pv:
        return
    lo = min(v["ts"] - (v["duration"] or 0) for v in pv) - CAM_BEFORE_S
    hi = max(v["ts"] for v in pv) + CAM_AFTER_S
    by_robot = defaultdict(list)
    for d in db().execute("SELECT robot_serial, ts, pet_id, distance FROM camera_detections "
                          "WHERE ts >= ? AND ts <= ? AND pet_id IS NOT NULL ORDER BY ts", (lo, hi)):
        by_robot[d["robot_serial"]].append(d)
    for v in pv:
        rows = by_robot.get(v["robot"], [])
        times = [d["ts"] for d in rows]
        a = bisect.bisect_left(times, v["ts"] - (v["duration"] or 0) - CAM_BEFORE_S)
        b = bisect.bisect_right(times, v["ts"] + CAM_AFTER_S)
        hits = [d for d in rows[a:b] if d["distance"] is not None]
        best = min(hits, key=lambda d: d["distance"], default=None)
        v["camera"] = ({"pet": best["pet_id"], "similarity": round(1 - best["distance"], 3), "count": len(hits),
                        "agrees": best["pet_id"] == v["pet"]} if best else None)
        if detail:
            v["camera_detections"] = [{"ts": d["ts"], "pet": d["pet_id"], "similarity": round(1 - d["distance"], 3)}
                                      for d in hits]


def _clip_matches(visits):
    """Yield (visit, the clips recorded around it), including clips whose video retention removed."""
    if not visits:
        return
    lo = min(v["ts"] for v in visits) - MATCH_BEFORE_S - 600
    hi = max(v["ts"] for v in visits) + MATCH_AFTER_S + 1
    rows = clip_rows(lo, hi)
    times = [c["ts"] for c in rows]
    for v in visits:
        a = bisect.bisect_left(times, v["ts"] - MATCH_BEFORE_S - (v["duration"] or 0))
        b = bisect.bisect_right(times, v["ts"] + MATCH_AFTER_S)
        yield v, [c for c in rows[a:b] if v["robot"] in (None, c["robot_serial"], c["r_serial"])]


def _visits_with_clips(visits) -> set:
    return {v["id"] for v, matched in _clip_matches(visits) if matched}


def attach_clip_summary(visits):
    for v, matched in _clip_matches(visits):
        v["clip_count"] = len(matched)
        best = min(matched, key=lambda c: abs(c["ts"] - (v["ts"] - (v["duration"] or 0))), default=None)
        v["thumb"] = media_url("media", best["robot_folder"], best["thumb_file"]) if best else None


# ------------------------------------------------------------ events API


@app.route("/api/meta")
def api_meta():
    pets = load_pets()
    refs = reference_weights(pets)
    for p in pets:
        p["ref_weight"] = refs.get(p["id"])
    robots = [dict(r) for r in db().execute("SELECT serial, name, folder, timezone FROM robots ORDER BY name")]
    types = [r["type"] for r in db().execute("SELECT DISTINCT type FROM activities ORDER BY type")]
    last = db().execute("SELECT MAX(ts) AS ts FROM activities").fetchone()["ts"]
    return jsonify(pets=pets, robots=robots, types=types, last_activity=last,
                   match_window={"before": MATCH_BEFORE_S, "after": MATCH_AFTER_S},
                   writeback=lr_db.writeback_enabled(db()))


def _date_bound(value: str | None, tz, end=False):
    if not value:
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        abort(400)
    return (d + timedelta(days=1 if end else 0)).timestamp()


@app.route("/api/events")
def api_events():
    tz = local_tz()
    kind = request.args.get("type", "PET_VISIT")
    types = None if kind == "ALL" else (kind,)
    visits = load_visits(_date_bound(request.args.get("from"), tz),
                         _date_bound(request.args.get("to"), tz, end=True),
                         request.args.get("robot") or None, types)
    pets = load_pets()
    refs = reference_weights(pets)
    active = {p["id"] for p in pets if p["active"]}
    for v in visits:
        annotate(v, refs, active)

    pet = request.args.get("pet")
    if pet == "none":
        visits = [v for v in visits if v["type"] == "PET_VISIT" and not v["pet"] and not v["ignored"]]
    elif pet:
        visits = [v for v in visits if v["pet"] == pet]
    flt = request.args.get("filter")
    if flt == "review":
        visits = [v for v in visits if v["review"]]
    elif flt == "corrected":
        visits = [v for v in visits if v["corrected"]]
    elif flt == "ignored":
        visits = [v for v in visits if v["ignored"]]

    total = len(visits)
    size = min(int(request.args.get("size", 100)), 500)
    page = max(int(request.args.get("page", 0)), 0)
    visits = visits[page * size:(page + 1) * size]
    attach_clip_summary(visits)
    attach_camera(visits)
    return jsonify(total=total, page=page, size=size, events=visits)


def _one_event(event_id):
    row = db().execute("SELECT ts FROM activities WHERE id = ?", (event_id,)).fetchone()
    if not row:
        abort(404)
    v = next(x for x in load_visits(row["ts"], row["ts"] + 0.001, types=None) if x["id"] == event_id)
    p = lr_db.latest_push(db(), event_id)
    v["push"] = ({"status": p["status"], "error": p["error"], "to_pet": p["to_pet"], "at": p["updated_at"]}
                 if p else None)
    v["whisker_editable"] = lr_db.whisker_editable(v["ts"])
    attach_camera([v], detail=True)
    pets = load_pets()
    return annotate(v, reference_weights(pets), {p["id"] for p in pets if p["active"]})


@app.route("/api/events/<event_id>")
def api_event(event_id):
    v = _one_event(event_id)
    rows = clip_rows(v["ts"] - NEARBY_S, v["ts"] + NEARBY_S)
    rows = [c for c in rows if v["robot"] in (None, c["robot_serial"], c["r_serial"])]
    downloads = lr_db.clip_download_status(db(), [c["id"] for c in rows])
    clips = [clip_json(c, v["ts"], v["duration"], v["camera_event_ids"], downloads) for c in rows]
    anchor = v["ts"] - (v["duration"] or 0)
    clips.sort(key=lambda c: (not c["match"], abs(c["ts"] - anchor)))
    nearby = [x for x in load_visits(v["ts"] - NEARBY_S, v["ts"] + NEARBY_S, v["robot"], types=None)
              if x["id"] != event_id]
    return jsonify(event=v, clips=clips, nearby=nearby)


@app.route("/api/events/<event_id>/override", methods=["POST", "DELETE"])
def api_override(event_id):
    conn = db()
    act = conn.execute("SELECT type, ts, pet_ids, original_pet_ids, pet_weight FROM activities WHERE id = ?",
                       (event_id,)).fetchone()
    if not act:
        abort(404)
    # too light to be a cat: notes can change, the cat can't (and Whisker keeps no cat)
    light = act["type"] == "PET_VISIT" and act["pet_weight"] is not None and act["pet_weight"] < IGNORE_UNDER_LB
    whisker_pet = lr_db.first_pet(act["pet_ids"])
    robot_pet = lr_db.first_pet(act["original_pet_ids"] or act["pet_ids"])
    push_to = ...  # Ellipsis = no Whisker change
    if request.method == "DELETE":
        conn.execute("DELETE FROM visit_overrides WHERE activity_id = ?", (event_id,))
        push_to = ... if light else robot_pet
    else:
        body = request.get_json(force=True, silent=True) or {}
        cur = conn.execute("SELECT * FROM visit_overrides WHERE activity_id = ?", (event_id,)).fetchone()
        pet_id = cur["pet_id"] if cur else None
        note = cur["note"] if cur else None
        if "pet_id" in body and light and body["pet_id"] not in (None, "", lr_db.UNKNOWN_PET):
            return jsonify(reason="too_light", error=f"Visits under {IGNORE_UNDER_LB} lb can't be given a cat."), 409
        if "pet_id" in body:
            pet_id = body["pet_id"] or None
            if pet_id not in (None, lr_db.UNKNOWN_PET) and not conn.execute(
                    "SELECT 1 FROM pets WHERE pet_id = ?", (pet_id,)).fetchone():
                abort(400)
            push_to = None if pet_id == lr_db.UNKNOWN_PET else (pet_id or whisker_pet)
            if pet_id == whisker_pet:
                pet_id = None
        if "note" in body:
            note = (body["note"] or "").strip()[:500] or None
        if pet_id is None and not note:
            conn.execute("DELETE FROM visit_overrides WHERE activity_id = ?", (event_id,))
        else:
            conn.execute(
                "INSERT INTO visit_overrides (activity_id, pet_id, note, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(activity_id) DO UPDATE SET pet_id=excluded.pet_id, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (event_id, pet_id, note, time.time()))
    if (push_to is not ... and act["type"] == "PET_VISIT" and lr_db.writeback_enabled(conn)
            and lr_db.whisker_editable(act["ts"])
            and (push_to != whisker_pet or lr_db.latest_push(conn, event_id))):
        lr_db.enqueue_push(conn, event_id, push_to)
    conn.commit()
    lr_db.wake()
    return jsonify(event=_one_event(event_id))


@app.route("/api/events/<event_id>/push", methods=["POST"])
def api_retry_push(event_id):
    """Re-send this visit's current cat to Whisker (retry after a failure)."""
    conn = db()
    v = _one_event(event_id)
    if v["type"] != "PET_VISIT":
        abort(400)
    if not lr_db.whisker_editable(v["ts"]):
        return jsonify(reason="too_old", event=v), 409
    lr_db.enqueue_push(conn, event_id, v["pet"])
    conn.commit()
    lr_db.wake()
    return jsonify(event=_one_event(event_id))


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    conn = db()
    wake_collector = False
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        if "writeback" in body:
            lr_db.set_state(conn, "writeback", "1" if body["writeback"] else "0")
        if "unit" in body:
            if body["unit"] not in ("kg", "lb"):
                abort(400)
            lr_db.set_state(conn, "weight_unit", body["unit"])
        if "retention_days" in body or "retention_small" in body:
            before = lr_db.retention_tiers(conn)
            if "retention_days" in body:
                days = body["retention_days"]
                if days not in lr_db.RETENTION_CHOICES or 0 < days < int(_fstate("retention_min_days")):
                    abort(400)
                lr_db.set_state(conn, "retention_setting", str(days))
            if "retention_small" in body:
                if body["retention_small"] not in lr_db.SMALL_CHOICES:
                    abort(400)
                lr_db.set_state(conn, "retention_small", str(body["retention_small"]))
            if _harsher(lr_db.retention_tiers(conn), before):
                # clips will be shrunk or lose their video sooner: restart the grace period
                # (and its banner) so the ones about to change can be marked Keep forever first
                lr_db.set_state(conn, "retention_enabled_at", str(time.time()))
        if "sync_interval" in body:
            if body["sync_interval"] not in lr_db.SYNC_INTERVALS:
                abort(400)
            lr_db.set_state(conn, "sync_interval", str(body["sync_interval"]))
            wake_collector = True
        if "clip_tz" in body:
            if body["clip_tz"] not in lr_db.CLIP_TZ_CHOICES:
                abort(400)
            lr_db.set_state(conn, "clip_tz", body["clip_tz"])
        if "notify" in body:
            import lr_notify
            try:
                lr_notify.save_settings(conn, body["notify"] or {})
            except ValueError as err:
                return jsonify(error=str(err)), 400
        conn.commit()
        if wake_collector:
            lr_db.wake()  # so a waiting collector picks up the new interval now
    # only visits Whisker still accepts changes to; older ones can't be retried anyway
    editable = time.time() - lr_db.WHISKER_EDIT_WINDOW_S
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM whisker_pushes p JOIN activities a ON a.id = p.activity_id "
        "WHERE p.status IN ('pending', 'sending') AND a.ts >= ?", (editable,)).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM whisker_pushes p JOIN activities a ON a.id = p.activity_id "
        "WHERE p.status = 'error' AND a.ts >= ? AND p.id = "
        "(SELECT MAX(id) FROM whisker_pushes q WHERE q.activity_id = p.activity_id)", (editable,)).fetchone()["n"]
    return jsonify(writeback=lr_db.writeback_enabled(conn), unit=weight_unit(), pending=pending, failed=failed,
                   retention=retention_info(), clip_tz=lr_db.clip_list_tz(conn),
                   server_tz=local_timezone_name(), notify=_notify_settings(conn),
                   sync_interval=lr_db.sync_interval(conn, int(os.environ.get("LR_DOWNLOAD_INTERVAL", "3600"))),
                   sync_intervals=lr_db.SYNC_INTERVALS, retention_choices=lr_db.RETENTION_CHOICES,
                   small_choices=lr_db.SMALL_CHOICES, access={**lr_auth.describe(), "you": g.user.json()})


def _harsher(new, old) -> bool:
    """Whether retention tiers `new` shrink clips or drop their video sooner than `old`."""
    inf = float("inf")
    full = lambda t: t[0] or inf
    video = lambda t: inf if not t[0] else (t[0] if t[1] is None else (t[1] or inf))
    return full(new) < full(old) or video(new) < video(old)


def _notify_settings(conn) -> dict:
    import lr_notify
    cfg = lr_notify.settings(conn)
    # the token and key never go back to the browser; the page shows the last few characters
    return {"enabled": cfg["enabled"], "link": cfg["link"], "lead_days": cfg["lead_days"],
            "categories": [{"id": cid, "group": grp, "label": label, "detail": detail, "on": cid not in cfg["off"]}
                           for cid, grp, label, detail in lr_notify.CATEGORIES],
            "user_hint": cfg["user"][-4:] if cfg["user"] else None,
            "token_hint": cfg["token"][-4:] if cfg["token"] else None,
            "last_sent": cfg["last_sent"], "last_error": cfg["last_error"]}


LOG_TAIL_BYTES = 512 * 1024


def _log_path(source: str) -> Path:
    if source not in lr_db.LOG_FILES:
        abort(404)
    return lr_db.LOG_DIR / lr_db.LOG_FILES[source]


@app.route("/api/logs/<source>")
def api_logs(source):
    path = _log_path(source)
    limit = max(1, min(request.args.get("lines", 500, type=int), 5000))
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read()
    except FileNotFoundError:
        return jsonify(lines=[], exists=False)
    lines = data.decode("utf-8", "replace").splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # first line is probably cut in half
    return jsonify(lines=lines[-limit:], exists=True, size=size, modified=path.stat().st_mtime)


@app.route("/api/logs/<source>/download")
def api_logs_download(source):
    path = _log_path(source)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="text/plain", as_attachment=True,
                     download_name=f"litterrobot-{lr_db.LOG_FILES[source]}", max_age=0)


# ------------------------------------------------------------ analysis API


def _median(xs):
    return statistics.median(xs) if xs else None


def _fmt_dur(s: float) -> str:
    s = round(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


# ------------------------------------------------------------ per-cat baselines

BASELINE_DAYS = 30  # each cat's normal is learned from this many days...
RECENT_DAYS = 7     # ...ending this long ago, so a current change doesn't blend into its own baseline
NO_WASTE = (None, "", "Unknown")


def _spread(xs, center):
    """Robust standard deviation: the median absolute deviation, scaled to match a normal sd."""
    return 1.4826 * statistics.median(abs(x - center) for x in xs)


def cat_baselines(pets, refs, active, now, tz, end=None):
    """Each cat's own normal range (visits per day, visit length, weight) and how the
    RECENT_DAYS before `end` (default now) compare. Uses every robot, since a cat's
    habits span all of them."""
    end = end or now
    first = db().execute("SELECT MIN(ts) AS ts FROM activities").fetchone()["ts"] or end
    recent_start = end - RECENT_DAYS * 86400
    base_start = max(recent_start - BASELINE_DAYS * 86400, first)
    visits = [annotate(v, refs, active) for v in load_visits(base_start, end, types=("PET_VISIT",))
              if not v["ignored"]]
    # whole local days inside the baseline window (a partial first day would undercount)
    day = datetime.fromtimestamp(base_start, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if day.timestamp() < base_start:
        day += timedelta(days=1)
    days = []
    while (day + timedelta(days=1)).timestamp() <= recent_start:
        days.append(day.date())
        day += timedelta(days=1)
    didx = {d: i for i, d in enumerate(days)}

    out = {}
    for p in pets:
        pid = p["id"]
        mine = [v for v in visits if v["pet"] == pid]
        base = [v for v in mine if v["ts"] < recent_start]
        recent = [v for v in mine if v["ts"] >= recent_start]
        b = {"days": len(days), "ready": len(days) >= 10 and len(base) >= 5}
        if not b["ready"]:
            out[pid] = b
            continue

        counts = [0] * len(days)
        for v in base:
            i = didx.get(datetime.fromtimestamp(v["ts"], tz).date())
            if i is not None:
                counts[i] += 1
        mean, sd = statistics.fmean(counts), statistics.pstdev(counts)
        half = max(2 * sd / math.sqrt(RECENT_DAYS), 0.5)  # range for a week's average
        b["visits"] = {"mean": round(mean, 2), "low": round(max(0, mean - half), 2), "high": round(mean + half, 2),
                       "recent": round(len(recent) / RECENT_DAYS, 2),
                       "last24": sum(1 for v in mine if v["ts"] >= end - 86400),
                       "day_high": round(mean + max(3 * sd, 2), 1)}

        durs = [v["duration"] for v in base if v["duration"]]
        if len(durs) >= 8:
            m = statistics.median(durs)
            sp = max(_spread(durs, m), 0.1 * m, 5)
            long_over = max(m + 3 * sp, 1.5 * m)
            long = [v for v in recent if (v["duration"] or 0) > long_over]
            b["duration"] = {"median": round(m), "low": round(max(0, m - 2 * sp)), "high": round(m + 2 * sp),
                             "long_over": round(long_over),
                             "long": [{"ts": v["ts"], "duration": v["duration"], "waste": v["waste_type"] or None}
                                      for v in long]}

        # readings that look like another cat (unreviewed) would skew the range
        ws = [v["weight"] for v in base if v["weight"] and not v["review"]]
        rws = [v["weight"] for v in recent if v["weight"] and not v["review"]]
        if len(ws) >= 5:
            m = statistics.median(ws)
            sp = max(_spread(ws, m), 0.01 * m)
            half = max(2.5 * sp / math.sqrt(max(3, len(rws))), 0.02 * m)
            b["weight"] = {"median": round(m, 3), "low": round(m - half, 3), "high": round(m + half, 3),
                           "recent": round(statistics.median(rws), 3) if len(rws) >= 3 else None}
        out[pid] = b
    return out


def baseline_insights(name, b, unit, weight_flagged):
    """Plain-language flags where a cat is outside its own normal range, as (kind, text)."""
    out = []
    v = b.get("visits")
    if v:
        if v["last24"] > v["day_high"] and v["last24"] >= 4:
            out.append(("visits_burst", f"{name} visited {v['last24']} times in the last 24 hours (usually about {v['mean']:.1f}/day). "
                       f"Many visits in a short time can be a sign of a urinary problem; if {name} is straining "
                       f"or producing little, call your vet."))
        elif not v["low"] <= v["recent"] <= v["high"]:
            word = "more" if v["recent"] > v["high"] else "less"
            out.append(("visits_" + word, f"{name} is visiting {word} often than usual: {v['recent']:.1f}/day "
                                          f"over the last {RECENT_DAYS} days, against a usual "
                                          f"{v['low']:.1f}–{v['high']:.1f}/day."))
    d = b.get("duration")
    if d and d["long"]:
        n = len(d["long"])
        empty = sum(1 for x in d["long"] if x["waste"] in NO_WASTE)
        recent3 = sum(1 for x in d["long"] if x["ts"] >= time.time() - 3 * 86400)
        if n >= 3 or recent3 >= 2 or empty >= 2:
            msg = (f"{name} had {n} unusually long visit{'s' if n != 1 else ''} in the last {RECENT_DAYS} days "
                   f"(over {_fmt_dur(d['long_over'])}; typical is {_fmt_dur(d['median'])})")
            if empty:
                msg += f", {empty} with no waste detected. Long visits with little output can mean straining"
            out.append(("long_visits", msg + "."))
    w = b.get("weight")
    if w and w["recent"] is not None and not weight_flagged and not w["low"] <= w["recent"] <= w["high"]:
        word = "below" if w["recent"] < w["low"] else "above"
        out.append(("weight_" + word, f"{name}'s weight over the last {RECENT_DAYS} days "
                                      f"({fmt_weight(w['recent'], unit)}) is {word} the usual range "
                                      f"({fmt_weight(w['low'], unit)}–{fmt_weight(w['high'], unit)})."))
    return out


@app.route("/api/analysis")
def api_analysis():
    tz = local_tz()
    days = request.args.get("days", "30")
    robot = request.args.get("robot") or None
    now = time.time()
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if days == "all":
        first = db().execute("SELECT MIN(ts) AS ts FROM activities").fetchone()["ts"] or now
        n_days = max(1, (today - datetime.fromtimestamp(first, tz).replace(
            hour=0, minute=0, second=0, microsecond=0)).days + 1)
    else:
        n_days = max(1, min(int(days), 3650))
    start_dt = today - timedelta(days=n_days - 1)
    start = start_dt.timestamp()

    pets = load_pets()
    pet_ids = [p["id"] for p in pets]
    names = {p["id"]: p["name"] for p in pets}
    refs = reference_weights(pets)
    active = {p["id"] for p in pets if p["active"]}
    everything = [v for v in load_visits(start - n_days * 86400, None, robot, types=None) if not v["ignored"]]
    in_range = [annotate(v, refs, active) for v in everything if v["ts"] >= start]
    visits = [v for v in in_range if v["type"] == "PET_VISIT"]
    prev_visits = [v for v in everything if v["ts"] < start and v["type"] == "PET_VISIT"]

    dates = [(start_dt + timedelta(days=i)).date() for i in range(n_days)]
    didx = {d: i for i, d in enumerate(dates)}
    keys = pet_ids + ["none"]
    daily = {k: [0] * n_days for k in keys}
    hourly = {k: [0] * 24 for k in keys}
    heat = [[0] * 24 for _ in range(7)]
    durations = defaultdict(list)
    waste = defaultdict(Counter)
    weights = defaultdict(list)
    all_weights = defaultdict(list)
    for v in visits:
        k = v["pet"] if v["pet"] in names else "none"
        lt = datetime.fromtimestamp(v["ts"], tz)
        if lt.date() in didx:
            daily[k][didx[lt.date()]] += 1
        hourly[k][lt.hour] += 1
        heat[lt.weekday()][lt.hour] += 1
        if v["duration"]:
            durations[k].append(v["duration"])
        waste[k][v["waste_type"] or "Unknown"] += 1
        if v["weight"] and k != "none":
            all_weights[k].append((v["ts"], v["weight"]))
            # a reading that looks like a different cat (unreviewed) would skew trends
            if not v["review"]:
                weights[k].append((v["ts"], v["weight"]))

    # weight series: raw visit readings, daily medians, and Whisker's official history
    weight_series = {}
    official = defaultdict(list)
    for r in db().execute("SELECT pet_id, ts, weight FROM pet_weights WHERE ts >= ? ORDER BY ts", (start,)):
        official[r["pet_id"]].append([r["ts"] * 1000, r["weight"]])
    for pid in pet_ids:
        pts = sorted(weights.get(pid, []))
        by_day = defaultdict(list)
        for ts, w in pts:
            by_day[datetime.fromtimestamp(ts, tz).date()].append(w)
        weight_series[pid] = {
            "visits": [[ts * 1000, w] for ts, w in sorted(all_weights.get(pid, []))],
            "daily": [[datetime.combine(d, datetime.min.time(), tz).timestamp() * 1000 + 43200000,
                       round(statistics.median(ws), 2)] for d, ws in sorted(by_day.items())],
            "official": official.get(pid, []),
        }

    # per-cat KPIs
    last7 = now - 7 * 86400
    kpis = []
    for p in pets:
        pid = p["id"]
        mine = [v for v in visits if v["pet"] == pid]
        prev = [v for v in prev_visits if v["pet"] == pid]
        pts = weights.get(pid, [])
        span = min(7 * 86400, n_days * 86400 / 3)
        early = [w for ts, w in pts if ts < start + span]
        late = [w for ts, w in pts if ts >= now - span]
        cur_w = _median(late) or p["profile_weight"]
        change = (round(_median(late) - _median(early), 2)
                  if early and late and n_days >= 7 else None)
        last_visit = max((v["ts"] for v in mine), default=None)
        kpis.append({
            "pet": pid, "visits": len(mine), "per_day": round(len(mine) / n_days, 2),
            "prev_per_day": round(len(prev) / n_days, 2) if prev else None,
            "last7": sum(1 for v in mine if v["ts"] >= last7),
            "median_duration": _median([v["duration"] for v in mine if v["duration"]]),
            "weight": round(cur_w, 2) if cur_w else None, "weight_change": change,
            "weight_change_pct": round(100 * change / _median(early), 1) if change is not None else None,
            "waste": dict(waste.get(pid, {})), "last_visit": last_visit,
        })

    # robot health
    cycles_daily = [0] * n_days
    offline, interrupted = [], 0
    for v in in_range:
        lt = datetime.fromtimestamp(v["ts"], tz)
        if v["type"] == "CYCLE_COMPLETED" and lt.date() in didx:
            cycles_daily[didx[lt.date()]] += 1
        elif v["type"] == "OFFLINE":
            offline.append(v["ts"])
        elif v["type"] == "CYCLE_INTERRUPTED":
            interrupted += 1
    snaps = defaultdict(lambda: {"litter": [], "drawer": []})
    snap_sql = "SELECT * FROM robot_snapshots WHERE ts >= ?" + (" AND robot_serial = ?" if robot else "") + " ORDER BY ts"
    for r in db().execute(snap_sql, (start, robot) if robot else (start,)):
        if r["litter_level"] is not None:
            snaps[r["robot_serial"]]["litter"].append([r["ts"] * 1000, r["litter_level"]])
        if r["waste_drawer_level"] is not None:
            snaps[r["robot_serial"]]["drawer"].append([r["ts"] * 1000, r["waste_drawer_level"]])

    # corrections
    pairs = Counter()
    corrected = 0
    for v in in_range:
        if v["type"] == "PET_VISIT" and v["corrected"]:
            corrected += 1
            if v["corrected"]:
                pairs[(v["api_pet"] or "none", v["pet"] or "unknown")] += 1
    auto_ided = sum(1 for v in in_range if v["type"] == "PET_VISIT" and v["api_pet"])

    baselines = cat_baselines(pets, refs, active, now, tz)
    for k in kpis:
        k["baseline"] = baselines.get(k["pet"])
    insights = build_insights(pets, kpis, visits, hourly, n_days, corrected, auto_ided, pairs,
                              names, offline, interrupted, cycles_daily, now, tz, baselines)

    return jsonify(
        pets=pets, days=n_days, start=start * 1000, tz=str(tz),
        dates=[d.isoformat() for d in dates],
        totals={"visits": len(visits), "unassigned": sum(daily["none"]),
                "per_day": round(len(visits) / n_days, 2),
                "cycles": sum(cycles_daily), "offline": len(offline), "interrupted": interrupted,
                "corrected": corrected},
        kpis=kpis, daily=daily, hourly=hourly, heatmap=heat, weights=weight_series,
        durations={k: round(_median(v), 1) for k, v in durations.items()},
        cycles_daily=cycles_daily,
        snapshots={s: {"name": next((r["name"] for r in db().execute(
            "SELECT name FROM robots WHERE serial = ?", (s,))), s), **d} for s, d in snaps.items()},
        mixups=[{"from": a, "to": b, "count": n} for (a, b), n in pairs.most_common(8)],
        insights=insights,
    )


def build_insights(pets, kpis, visits, hourly, n_days, corrected, auto_ided, pairs, names,
                   offline, interrupted, cycles_daily, now, tz, baselines):
    unit = weight_unit()
    out = []
    by_pet = {k["pet"]: k for k in kpis}
    for p in pets:
        if p["active"]:
            out += [("warn", t, p["id"], kind) for kind, t in _cat_insights(
                p, by_pet[p["id"]], baselines.get(p["id"]) or {}, kpis, visits, n_days, now, unit)]
    total = [sum(hourly[k][h] for k in hourly) for h in range(24)]
    if sum(total):
        peak = max(range(24), key=lambda h: total[h])
        quiet = min(range(24), key=lambda h: total[h])
        out.append(("info", f"Busiest hour is {peak:02d}:00–{(peak + 1) % 24:02d}:00 "
                            f"({100 * total[peak] / sum(total):.0f}% of visits); quietest is {quiet:02d}:00."))
    if auto_ided:
        rate = 100 * corrected / auto_ided
        msg = f"You've corrected {corrected} of {auto_ided} auto-identified visits ({rate:.0f}%)."
        if pairs:
            (a, b), n = pairs.most_common(1)[0]
            msg += (f" Most common mix-up: logged as {names.get(a, 'no cat')} but was actually "
                    f"{names.get(b, 'an unknown cat')} ({n}×).")
        out.append(("info", msg))
    if sum(cycles_daily):
        out.append(("info", f"The robot ran {sum(cycles_daily) / n_days:.1f} clean cycles per day "
                            f"({sum(cycles_daily) / max(1, len(visits)):.2f} per visit)."))
    if offline:
        out.append(("warn", f"The robot went offline {len(offline)} time{'s' if len(offline) != 1 else ''}, "
                            f"most recently {datetime.fromtimestamp(max(offline), tz).strftime('%b %d %H:%M')}."))
    order = {"warn": 0, "info": 1}
    out = [x if len(x) == 4 else (*x, None, None) for x in out]
    return [{"level": lvl, "text": t, "pet": pet, "kind": kind}
            for lvl, t, pet, kind in sorted(out, key=lambda x: order[x[0]])]


def _cat_insights(p, k, b, kpis, visits, n_days, now, unit) -> list[tuple[str, str]]:
    """Warnings about one cat as (kind, text); the Cats page shows them behind the ⚠ on its
    card, and notifications use the kind to tell a new problem from one already sent."""
    out = []
    name = p["name"]
    if k["last_visit"] is None:
        if k["prev_per_day"]:
            out.append(("no_visits", f"{name} has no recorded visits in this period."))
        return out
    hours = (now - k["last_visit"]) / 3600
    if hours >= 24 and k["per_day"] >= 1:
        out.append(("no_visit_hours", f"{name} hasn't visited in {hours:.0f} hours (usually {k['per_day']:.1f}×/day)."))
    if not b.get("ready"):
        # no baseline yet: fall back to comparing with the week before
        week = 7 * 86400
        mine = [v["ts"] for v in visits if v["pet"] == p["id"]]
        this_wk = sum(1 for t in mine if t >= now - week) / 7
        last_wk = sum(1 for t in mine if now - 2 * week <= t < now - week) / 7
        if n_days >= 14 and last_wk:
            delta = (this_wk - last_wk) / last_wk
            if abs(delta) >= 0.4 and abs(this_wk - last_wk) >= 1:
                word = "up" if delta > 0 else "down"
                out.append(("visits_" + word, f"{name}'s visits are {word} {abs(delta) * 100:.0f}% this week "
                                              f"({this_wk:.1f}/day vs {last_wk:.1f}/day the week before)."))
    weight_flagged = k["weight_change_pct"] is not None and abs(k["weight_change_pct"]) >= 5
    if weight_flagged:
        pct, ch = k["weight_change_pct"], k["weight_change"]
        out.append(("weight_change", f"{name}'s weight changed {fmt_weight(ch, unit, signed=True)} ({pct:+.1f}%) "
                                     f"over this period. A sustained change of 5%+ is worth mentioning to your vet."))
    if b.get("ready"):
        out += baseline_insights(name, b, unit, weight_flagged)
    w = k["waste"]
    any_feces = any(x["waste"].get("Feces") for x in kpis)
    if any_feces and n_days >= 7 and sum(w.values()) >= 10 and not w.get("Feces"):
        out.append(("no_stool", f"{name} has no stool visits recorded in {n_days} days."))
    return out


# ------------------------------------------------------------ notifications


# which notification category (lr_notify.CATEGORIES) each kind of cat warning belongs to
CAT_CATEGORIES = {"visits_burst": "burst", "visits_more": "more_visits", "visits_less": "fewer_visits",
                  "visits_up": "more_visits", "visits_down": "fewer_visits",
                  "no_visit_hours": "no_visits", "no_visits": "no_visits", "long_visits": "long_visits",
                  "weight_change": "weight_change", "weight_below": "weight_range", "weight_above": "weight_range",
                  "no_stool": "no_stool"}
DRAWER_FAULTS = ("Waste drawer full", "Drawer-full indicator on")


def current_alerts() -> list[dict]:
    """Problems worth a push notification right now. `key` stays the same while a problem
    lasts, so lr_notify sends each one once. Needs a request context (lr_notify sets one up)."""
    out = []
    with app.test_request_context("/api/analysis?days=30"):
        data = api_analysis().get_json()
    names = {p["id"]: p["name"] for p in data["pets"]}
    for i in data["insights"]:
        if i["pet"] and i["kind"]:
            out.append({"key": f"cat:{i['pet']}:{i['kind']}", "group": "cats", "category": CAT_CATEGORIES[i["kind"]],
                        "subject": names.get(i["pet"], "A cat"), "text": i["text"],
                        "priority": 1 if i["kind"] == "visits_burst" else 0})
    for r in db().execute("SELECT serial, name, info FROM robots WHERE info IS NOT NULL"):
        info = json.loads(r["info"])
        problems = [(f"fault:{f}", "drawer_full" if f in DRAWER_FAULTS else "fault", f)
                    for f in info.get("faults") or []]
        if info.get("online") is False:
            problems.append(("offline", "offline", "Offline: the robot isn't connected to Whisker."))
        if info.get("litter_state") in ("LOW", "EMPTY", "REFILL"):
            problems.append(("litter", "litter",
                             f"Litter level is {info['litter_state'].lower()}; top up the hopper or globe."))
        out += [{"key": f"robot:{r['serial']}:{k}", "group": "robots", "category": cat, "subject": r["name"],
                 "text": t, "priority": 0} for k, cat, t in problems]
        out += [{"key": f"robot:{r['serial']}:{k}", "group": "iq", "category": cat, "subject": r["name"],
                 "text": t, "priority": 0} for k, cat, t in _iq_alerts(r["serial"])]
    return out


def _iq_alerts(serial) -> list[tuple[str, str, str]]:
    """Robot IQ forecasts that are due within the lead time set on the Settings page."""
    import lr_notify
    lead = lr_notify.settings(db())["lead_days"]
    tz = local_tz()
    on = lambda eta: datetime.fromtimestamp(eta, tz).strftime("%a %-d %b")
    days = lambda d: "less than a day" if d < 1 else f"about {d:.0f} day{'s' if round(d) != 1 else ''}"
    f = lr_iq.forecasts(db(), serial)
    out = []
    d = f["drawer"]
    if d and d.get("state") == "ok" and d["days"] <= lead:
        out.append(("drawer_soon", "drawer_soon", f"Waste drawer will be full in {days(d['days'])} "
                                                  f"(around {on(d['eta'])}) at the current rate."))
    h = f["hopper"]
    if h and h.get("state") == "empty":
        out.append(("hopper_empty", "hopper_soon", "Litter hopper is empty; refill it."))
    elif h and h.get("state") == "low":
        out.append(("hopper_low", "hopper_soon", "Litter hopper is low; refill it soon."))
    elif h and h.get("state") == "ok" and h["days"] <= lead:
        out.append(("hopper_soon", "hopper_soon", f"Litter hopper will need refilling in {days(h['days'])} "
                                                  f"(around {on(h['eta'])}) at the current rate."))
    return out


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Send a test notification with the keys typed on the Settings page (or the saved ones)."""
    import lr_notify
    body = request.get_json(force=True, silent=True) or {}
    conn = db()
    cfg = lr_notify.settings(conn)
    token, user = body.get("token") or cfg["token"], body.get("user") or cfg["user"]
    if not token or not user:
        return jsonify(error="Enter your user key and API token first."), 400
    try:
        lr_notify.send(token, user, "Litter Robot", "Test notification: Pushover is set up.",
                       url=cfg["link"] or None)
    except lr_notify.PushoverError as err:
        return jsonify(error=str(err)), 400
    return jsonify(ok=True)


# ------------------------------------------------------------ vet report


@app.route("/report/<pet_id>.pdf")
def vet_report(pet_id):
    import lr_report  # reportlab is only loaded when someone asks for a report
    tz = local_tz()
    today = datetime.now(tz).date()
    try:
        last = date.fromisoformat(request.args["to"]) if request.args.get("to") else today
        first = date.fromisoformat(request.args["from"]) if request.args.get("from") else last - timedelta(days=29)
    except ValueError:
        abort(400)
    last = min(last, today)
    if first > last or (last - first).days > 366:
        abort(400)
    pets = load_pets()
    pet = next((p for p in pets if p["id"] == pet_id), None)
    if not pet:
        abort(404)
    pdf = lr_report.build_pdf(vet_report_data(pet, pets, first, last, tz))
    slug = re.sub(r"[^A-Za-z0-9]+", "-", pet["name"]).strip("-") or "cat"
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{slug}-litter-report-{first}-to-{last}.pdf"'})


def vet_report_data(pet, pets, first: date, last: date, tz) -> dict:
    """Everything the vet report shows for one cat over [first, last] (local dates).
    Usual ranges come from the period plus up to BASELINE_DAYS before it, using robust
    statistics so the period's own outliers barely move them."""
    unit = weight_unit()
    conv = (lambda lb: lb * 0.453592) if unit == "kg" else (lambda lb: lb)
    fw = lambda lb: f"{conv(lb):.2f} {unit}"
    when = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%a %-d %b %H:%M")
    pid = pet["id"]
    start = datetime.combine(first, datetime.min.time(), tz).timestamp()
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tz).timestamp()
    refs = reference_weights(pets)
    active = {p["id"] for p in pets if p["active"]}
    data_start = db().execute("SELECT MIN(ts) AS ts FROM activities").fetchone()["ts"] or start
    pool_start = max(start - BASELINE_DAYS * 86400, data_start)
    pool = sorted((v for v in (annotate(v, refs, active) for v in load_visits(pool_start, end, types=("PET_VISIT",)))
                   if v["pet"] == pid and not v["ignored"]), key=lambda v: v["ts"])
    visits = [v for v in pool if v["ts"] >= start]

    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    didx = {d: i for i, d in enumerate(days)}
    daily = [{"visits": 0, "urine": 0, "feces": 0, "other": 0, "weights": [], "longest": None} for _ in days]
    readings = []
    for v in visits:
        lt = datetime.fromtimestamp(v["ts"], tz)
        i = didx.get(lt.date())
        if i is None:
            continue
        row = daily[i]
        row["visits"] += 1
        row[{"Urine": "urine", "Feces": "feces"}.get(v["waste_type"], "other")] += 1
        if v["duration"]:
            row["longest"] = max(row["longest"] or 0, v["duration"])
        if v["weight"] and not v["review"]:
            row["weights"].append(v["weight"])
            readings.append((i + (lt.hour + lt.minute / 60) / 24 - 0.5, round(conv(v["weight"]), 3)))
    for row in daily:
        row["weight"] = round(conv(statistics.median(row["weights"])), 3) if row["weights"] else None

    # the cat's usual: daily visit counts over whole days in the pool
    d0 = datetime.fromtimestamp(pool_start, tz).date()
    if datetime.combine(d0, datetime.min.time(), tz).timestamp() < pool_start:
        d0 += timedelta(days=1)
    pool_days = [d0 + timedelta(days=i) for i in range((last - d0).days + 1)]
    counts = Counter(datetime.fromtimestamp(v["ts"], tz).date() for v in pool)
    day_counts = [counts.get(d, 0) for d in pool_days] or [0]
    mean, sd = statistics.fmean(day_counts), statistics.pstdev(day_counts)
    durs = [v["duration"] for v in pool if v["duration"]]
    dm = statistics.median(durs) if len(durs) >= 8 else None
    dsp = max(_spread(durs, dm), 0.1 * dm, 5) if dm else None
    long_over = max(dm + 3 * dsp, 1.5 * dm) if dm else None
    ws = [v["weight"] for v in pool if v["weight"] and not v["review"]]
    wm = statistics.median(ws) if len(ws) >= 5 else None
    wsp = max(_spread(ws, wm), 0.01 * wm) if wm else None

    anomalies = []
    for d, row in zip(days, daily):
        if row["visits"] >= 4 and row["visits"] > mean + max(3 * sd, 2):
            anomalies.append(f"{d:%a %-d %b}: {row['visits']} visits (usually about {mean:.1f} a day).")
    long_visits = [v for v in visits if long_over and (v["duration"] or 0) > long_over]
    if long_visits:
        empty = sum(1 for v in long_visits if v["waste_type"] in NO_WASTE)
        msg = (f"{len(long_visits)} unusually long visit{'s' if len(long_visits) != 1 else ''} "
               f"(over {_fmt_dur(long_over)}; typical {_fmt_dur(dm)})")
        if empty:
            msg += f", {empty} with no waste detected. Long visits with little output can mean straining"
        anomalies.append(msg + ". Listed below.")
    if mean >= 0.7:
        # a gap counts when it's at least 3× the cat's usual time between visits (and a day)
        min_gap = max(24, 3 * 24 / mean) * 3600
        gaps = sorted(((b["ts"] - a["ts"], a["ts"], b["ts"]) for a, b in zip(visits, visits[1:])
                       if b["ts"] - a["ts"] >= min_gap), reverse=True)
        if len(gaps) > 3:
            g, a, b = gaps[0]
            anomalies.append(f"{len(gaps)} gaps of {min_gap / 3600:.0f}+ hours without a visit; the longest was "
                             f"{g / 3600:.0f} hours, from {when(a)} to {when(b)}.")
        else:
            anomalies += [f"No visits for {g / 3600:.0f} hours, from {when(a)} to {when(b)}." for g, a, b in gaps]
    if wm:
        band = max(3 * wsp, 0.04 * wm)
        off = [(d, row) for d, row in zip(days, daily)
               if len(row["weights"]) >= 2 and abs(statistics.median(row["weights"]) - wm) > band]
        if off:
            shown = ", ".join(f"{d:%-d %b} ({row['weight']:.2f})" for d, row in off[:6])
            anomalies.append(f"Daily weight outside the usual {fw(wm - band)}–{fw(wm + band)} on {len(off)} "
                             f"day{'s' if len(off) != 1 else ''}: {shown}{'…' if len(off) > 6 else ''}.")
    wk = 7 * 86400
    early = [v["weight"] for v in visits if v["weight"] and not v["review"] and v["ts"] < start + wk]
    late = [v["weight"] for v in visits if v["weight"] and not v["review"] and v["ts"] >= end - wk]
    change = None
    if len(early) >= 3 and len(late) >= 3 and len(days) >= 14:
        e, l = statistics.median(early), statistics.median(late)
        change = (l - e, 100 * (l - e) / e)
        if abs(change[1]) >= 5:
            anomalies.append(f"Weight changed {conv(change[0]):+.2f} {unit} ({change[1]:+.1f}%) "
                             f"from the first week to the last.")
    # longest run of days without a stool visit, if the cat normally has them
    gap, run, run_end = 0, 0, None
    stool_days = {datetime.fromtimestamp(v["ts"], tz).date() for v in pool if v["waste_type"] == "Feces"}
    for d in days:
        run = 0 if d in stool_days else run + 1
        if run > gap:
            gap, run_end = run, d
    if stool_days and gap >= 3:
        anomalies.append(f"No stool visits for {gap} days in a row "
                         f"({run_end - timedelta(days=gap - 1):%-d %b}–{run_end:%-d %b}).")

    n = len(visits)
    tot = Counter({"urine": 0, "feces": 0, "other": 0})
    for row in daily:
        tot.update({k: row[k] for k in ("urine", "feces", "other")})
    pct = lambda k: f"{100 * tot[k] / n:.0f}%" if n else "—"
    photo = db().execute("SELECT photo_file FROM pets WHERE pet_id = ?", (pid,)).fetchone()
    return {
        "name": pet["name"], "unit": unit, "tz": str(tz), "start": first, "end": last, "days": days,
        "generated": datetime.now(tz),
        "photo": str(lr_db.photo_dir() / photo["photo_file"]) if photo and photo["photo_file"] else None,
        "summary": {
            "visits": n, "per_day": n / len(days),
            "usual_visits": f"about {mean:.1f} a day",
            "waste_split": f"{pct('urine')} / {pct('feces')} / {pct('other')}",
            "stool_gap": f"{gap} day{'s' if gap != 1 else ''}" if stool_days else "no stool visits recorded",
            "median_duration": _median([v["duration"] for v in visits if v["duration"]]),
            "usual_duration": f"{_fmt_dur(max(0, dm - 2 * dsp))}–{_fmt_dur(dm + 2 * dsp)}" if dm else "not enough data",
            "weight_first": fw(statistics.median(early)) if len(early) >= 3 else "—",
            "weight_last": fw(statistics.median(late)) if len(late) >= 3 else "—",
            "weight_change": f"{conv(change[0]):+.2f} {unit} ({change[1]:+.1f}%)" if change else "not enough data",
            "usual_weight": f"{fw(wm - 2 * wsp)}–{fw(wm + 2 * wsp)}" if wm else "not enough data",
        },
        "daily": daily, "daily_weight": [row["weight"] for row in daily], "weight_readings": readings,
        "weight_band": (conv(wm - 2 * wsp), conv(wm + 2 * wsp)) if wm else None,
        "anomalies": anomalies,
        "long_over": long_over, "typical": dm,
        "long_visits": [{"when": when(v["ts"]), "duration": v["duration"], "waste": v["waste_type"] or None,
                         "robot": v["robot_name"]} for v in long_visits],
        "notes": [{"when": when(v["ts"]), "note": v["note"]} for v in visits if v["note"]],
    }


# ------------------------------------------------------------ clips API (calendar page)


@app.route("/api/robots")
def list_robots():
    if not CLIPS.exists():
        return jsonify([])
    return jsonify(sorted(p.name for p in CLIPS.iterdir() if p.is_dir()))


def _robot_dir(robot) -> Path:
    root = os.path.realpath(CLIPS)
    d = os.path.realpath(os.path.join(root, robot))
    # only folders directly inside the clips folder are robots ("..", "." and the like aren't)
    if d.startswith(root + os.sep) and os.sep not in d[len(root) + 1:]:
        if os.path.isdir(d):
            return Path(d)
        abort(404)
    abort(403)


@app.route("/api/dates/<robot>")
def list_dates(robot):
    d = _robot_dir(robot)
    dates = set()
    for f in d.glob("*.mp4"):
        m = _DATE_RE.search(f.name)
        if m:
            dates.add(m.group(1))
    return jsonify(sorted(dates))


@app.route("/api/videos/<robot>/<date>")
def list_videos(robot, date):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(400)
    d = _robot_dir(robot)
    known ={r["video_file"]: r for r in db().execute(
        "SELECT * FROM clips WHERE robot_folder = ? AND video_file IS NOT NULL", (robot,))
        if f"_{date}_" in r["video_file"]}
    downloads = lr_db.clip_download_status(db(), [r["id"] for r in known.values()])
    videos = []
    for f in sorted(d.glob(f"*_{date}_*.mp4"), reverse=True):
        if f.name.endswith(".part.mp4"):
            continue
        m = _TIME_RE.search(f.name)
        row = known.get(f.name)
        videos.append({"filename": f.name, "time": m.group(1).replace("-", ":") if m else "",
                       "v": int(f.stat().st_mtime),
                       "id": row["id"] if row else None,
                       "download": downloads.get(str(row["id"])) if row else None,
                       **(keep_fields(row) if row else {})})
    return jsonify(videos)


def _clip_path(robot, filename) -> Path:
    filepath = (CLIPS / robot / filename).resolve()
    if not filepath.is_relative_to(CLIPS.resolve()):
        abort(403)
    if not filepath.is_file():
        abort(404)
    return filepath


@app.route("/media/<robot>/<filename>")
def serve_thumb(robot, filename):
    if not filename.lower().endswith(".jpg"):
        abort(404)
    return send_file(_clip_path(robot, filename), mimetype="image/jpeg", max_age=86400)


@app.route("/video/<robot>/<filename>")
def serve_video(robot, filename):
    filepath = _clip_path(robot, filename)
    size = filepath.stat().st_size
    range_header = request.headers.get("Range")

    if not range_header:
        resp = send_file(filepath, mimetype="video/mp4")
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    byte_range = range_header.replace("bytes=", "").split("-")
    start = int(byte_range[0]) if byte_range[0] else 0
    end = int(byte_range[1]) if len(byte_range) > 1 and byte_range[1] else size - 1
    end = min(end, size - 1)
    length = end - start + 1

    def stream():
        with open(filepath, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "video/mp4",
    }
    return Response(stream(), status=206, headers=headers, direct_passthrough=True)


# Loggers whose records go to web.log rather than the collector's sync.log.
LOGGERS = ("werkzeug", app.logger.name, lr_live.LOG.name, lr_auth.LOG.name)


def _not_health_check(record: logging.LogRecord) -> bool:
    """Leave Docker's and uptime monitors' health checks out of the console's request lines."""
    return "/api/health" not in record.getMessage()


logging.getLogger("werkzeug").addFilter(_not_health_check)


def serve() -> None:
    # Warnings and errors only; per-request lines still go to the console.
    try:
        handler = lr_db.log_file_handler("web")
        handler.setLevel(logging.WARNING)
        for name in LOGGERS:
            if name != lr_auth.LOG.name:
                logging.getLogger(name).addHandler(handler)
        # sessions and refusals have their own file (Settings > Logs > Access)
        lr_auth.LOG.setLevel(logging.INFO)
        lr_auth.LOG.addHandler(lr_db.log_file_handler("access"))
    except OSError as err:
        print(f"Not writing a web log file: {err}", flush=True)
    app.run(host="0.0.0.0", port=2287, debug=False, threaded=True)


if __name__ == "__main__":
    serve()
