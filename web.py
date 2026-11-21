#!/usr/bin/env python3
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   send_file, url_for)

from pylitterbot.camera import local_timezone_name

import lr_db
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
    return {"weight_unit": weight_unit()}


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


@app.route("/analysis")
def analysis_page():
    return render_template("analysis.html", page="analysis")


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
        last_visit=last_visit, recent=recent, latest=latest,
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
    return jsonify(_live(lr_live.start(serial)))


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
    _live(lr_live.close(sid))
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
    days = int(_fstate("retention_days"))
    enabled = _fstate("retention_enabled_at")
    grace = float(os.environ.get("LR_RETENTION_GRACE_HOURS", "24"))
    return {"days": days, "starts_at": enabled + grace * 3600 if days and enabled else None}


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
        })
    return out


def reference_weights(pets) -> dict[str, float]:
    """Each cat's typical weight: median of its (corrected) visit weights over the
    last 30 days, falling back to the Whisker profile weight."""
    since = time.time() - 30 * 86400
    by_pet = defaultdict(list)
    for v in load_visits(start=since):
        if v["pet"] and v["weight"]:
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
    if v["type"] != "PET_VISIT":
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
    days = retention_info()["days"]
    return {
        "keep": bool(c["keep"]), "deleted": bool(c["deleted_at"]),
        "expires": c["ts"] + days * 86400 if days and not c["keep"] and not c["deleted_at"] else None,
    }


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


def attach_clip_summary(visits):
    if not visits:
        return
    lo = min(v["ts"] for v in visits) - MATCH_BEFORE_S - 600
    hi = max(v["ts"] for v in visits) + MATCH_AFTER_S + 1
    rows = clip_rows(lo, hi)
    times = [c["ts"] for c in rows]
    for v in visits:
        a = bisect.bisect_left(times, v["ts"] - MATCH_BEFORE_S - (v["duration"] or 0))
        b = bisect.bisect_right(times, v["ts"] + MATCH_AFTER_S)
        matched = [c for c in rows[a:b] if v["robot"] in (None, c["robot_serial"], c["r_serial"])]
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
        visits = [v for v in visits if v["type"] == "PET_VISIT" and not v["pet"]]
    elif pet:
        visits = [v for v in visits if v["pet"] == pet]
    flt = request.args.get("filter")
    if flt == "review":
        visits = [v for v in visits if v["review"]]
    elif flt == "corrected":
        visits = [v for v in visits if v["corrected"]]

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
    act = conn.execute("SELECT type, ts, pet_ids, original_pet_ids FROM activities WHERE id = ?", (event_id,)).fetchone()
    if not act:
        abort(404)
    whisker_pet = lr_db.first_pet(act["pet_ids"])
    robot_pet = lr_db.first_pet(act["original_pet_ids"] or act["pet_ids"])
    push_to = ...  # Ellipsis = no Whisker change
    if request.method == "DELETE":
        conn.execute("DELETE FROM visit_overrides WHERE activity_id = ?", (event_id,))
        push_to = robot_pet
    else:
        body = request.get_json(force=True, silent=True) or {}
        cur = conn.execute("SELECT * FROM visit_overrides WHERE activity_id = ?", (event_id,)).fetchone()
        pet_id = cur["pet_id"] if cur else None
        note = cur["note"] if cur else None
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
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        if "writeback" in body:
            lr_db.set_state(conn, "writeback", "1" if body["writeback"] else "0")
        if "unit" in body:
            if body["unit"] not in ("kg", "lb"):
                abort(400)
            lr_db.set_state(conn, "weight_unit", body["unit"])
        if "clip_tz" in body:
            if body["clip_tz"] not in lr_db.CLIP_TZ_CHOICES:
                abort(400)
            lr_db.set_state(conn, "clip_tz", body["clip_tz"])
        conn.commit()
    pending = conn.execute("SELECT COUNT(*) AS n FROM whisker_pushes WHERE status IN ('pending', 'sending')").fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM whisker_pushes p WHERE status = 'error' AND id = "
        "(SELECT MAX(id) FROM whisker_pushes q WHERE q.activity_id = p.activity_id)").fetchone()["n"]
    return jsonify(writeback=lr_db.writeback_enabled(conn), unit=weight_unit(), pending=pending, failed=failed,
                   retention=retention_info(), clip_tz=lr_db.clip_list_tz(conn),
                   server_tz=local_timezone_name(),
                   sync_interval=int(os.environ.get("LR_DOWNLOAD_INTERVAL", "3600")))


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
    everything = load_visits(start - n_days * 86400, None, robot, types=None)
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

    insights = build_insights(pets, kpis, visits, hourly, n_days, corrected, auto_ided, pairs,
                              names, offline, interrupted, cycles_daily, now, tz)

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
                   offline, interrupted, cycles_daily, now, tz):
    unit = weight_unit()
    out = []
    by_pet = {k["pet"]: k for k in kpis}
    week = 7 * 86400
    for p in pets:
        if not p["active"]:
            continue
        k = by_pet[p["id"]]
        name = p["name"]
        if k["last_visit"] is None:
            if k["prev_per_day"]:
                out.append(("warn", f"{name} has no recorded visits in this period."))
            continue
        hours = (now - k["last_visit"]) / 3600
        if hours >= 24 and k["per_day"] >= 1:
            out.append(("warn", f"{name} hasn't visited in {hours:.0f} hours (usually {k['per_day']:.1f}×/day)."))
        mine = [v["ts"] for v in visits if v["pet"] == p["id"]]
        this_wk = sum(1 for t in mine if t >= now - week) / 7
        last_wk = sum(1 for t in mine if now - 2 * week <= t < now - week) / 7
        if n_days >= 14 and last_wk:
            delta = (this_wk - last_wk) / last_wk
            if abs(delta) >= 0.4 and abs(this_wk - last_wk) >= 1:
                word = "up" if delta > 0 else "down"
                out.append(("warn", f"{name}'s visits are {word} {abs(delta) * 100:.0f}% this week "
                                    f"({this_wk:.1f}/day vs {last_wk:.1f}/day the week before)."))
        if k["weight_change_pct"] is not None and abs(k["weight_change_pct"]) >= 5:
            pct, ch = k["weight_change_pct"], k["weight_change"]
            out.append(("warn", f"{name}'s weight changed {fmt_weight(ch, unit, signed=True)} ({pct:+.1f}%) over this period. "
                                f"A sustained change of 5%+ is worth mentioning to your vet."))
        w = k["waste"]
        any_feces = any(x["waste"].get("Feces") for x in kpis)
        if any_feces and n_days >= 7 and sum(w.values()) >= 10 and not w.get("Feces"):
            out.append(("warn", f"{name} has no stool visits recorded in {n_days} days."))
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
    return [{"level": lvl, "text": t} for lvl, t in sorted(out, key=lambda x: order[x[0]])]


# ------------------------------------------------------------ clips API (calendar page)


@app.route("/api/robots")
def list_robots():
    if not CLIPS.exists():
        return jsonify([])
    return jsonify(sorted(p.name for p in CLIPS.iterdir() if p.is_dir()))


@app.route("/api/dates/<robot>")
def list_dates(robot):
    d = CLIPS / robot
    if not d.is_dir():
        abort(404)
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
    d = CLIPS / robot
    if not d.is_dir():
        abort(404)
    known = {r["video_file"]: r for r in db().execute(
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
LOGGERS = ("werkzeug", app.logger.name, lr_live.LOG.name)


def serve() -> None:
    # Warnings and errors only; per-request lines still go to the console.
    try:
        handler = lr_db.log_file_handler("web")
        handler.setLevel(logging.WARNING)
        for name in LOGGERS:
            logging.getLogger(name).addHandler(handler)
    except OSError as err:
        print(f"Not writing a web log file: {err}", flush=True)
    app.run(host="0.0.0.0", port=2287, debug=False, threaded=True)


if __name__ == "__main__":
    serve()
