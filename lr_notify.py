"""Pushover notifications, checked after each sync.

Alerts come from web.current_alerts(): each cat's warnings on the Cats page and each
robot's current problems. A problem is sent once when it first appears; while it lasts
it isn't repeated, and once it clears it can be sent again if it comes back. New alerts
from one sync go out together as a single notification.

Visit notifications are separate and off unless switched on: the collector's status
loop sends one for each new visit soon after the robot's clean cycle finishes."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import lr_db

LOG = logging.getLogger("lr_notify")
API = "https://api.pushover.net/1/messages.json"
KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")  # Pushover user keys and API tokens
MAX_MESSAGE = 1024

# What can be switched on and off on the Settings page: (id, group, label, detail)
CATEGORIES = [
    ("visit", "cats", "Every visit", "soon after the robot cycles, with the cat Whisker matched"),
    ("burst", "cats", "Many visits in 24 hours", "can be a sign of a urinary problem; sent at high priority"),
    ("more_visits", "cats", "Visiting more often than usual", "last 7 days against the cat's own normal"),
    ("fewer_visits", "cats", "Visiting less often than usual", "last 7 days against the cat's own normal"),
    ("no_visits", "cats", "No visits for a day or more", "for cats that usually go at least daily"),
    ("long_visits", "cats", "Unusually long visits", "can mean straining, especially with no waste detected"),
    ("weight_change", "cats", "Weight change of 5% or more", "over the last 30 days"),
    ("weight_range", "cats", "Weight outside the usual range", "last 7 days against the cat's own normal"),
    ("no_stool", "cats", "No stool visits", "none recorded in 30 days"),
    ("drawer_full", "robots", "Waste drawer full", ""),
    ("fault", "robots", "Other faults", "bonnet or drawer removed, sensors, globe motor"),
    ("litter", "robots", "Litter low", ""),
    ("offline", "robots", "Robot offline", "not connected to Whisker"),
    ("drawer_soon", "iq", "Waste drawer full soon", "forecast from how fast it has been filling"),
    ("hopper_soon", "iq", "Litter hopper empty soon", "forecast from how fast litter is dispensed, or when the robot reports it low"),
]
DEFAULT_OFF = {"visit"}  # categories that stay off until switched on
LEAD_DAYS = (1, 2, 3, 5)  # how far ahead Robot IQ warns


class PushoverError(Exception):
    pass


def settings(conn) -> dict:
    get = lambda k, default=None: lr_db.get_state(conn, k) or default
    last_sent = get("notify_last_sent")
    switched_on = set(json.loads(get("notify_on", "[]")))  # DEFAULT_OFF categories switched on
    return {
        "enabled": get("notify_enabled") == "1",
        "off": set(json.loads(get("notify_off", "[]"))) | (DEFAULT_OFF - switched_on),  # category ids switched off
        "lead_days": int(get("notify_lead_days", "2")),
        "token": get("pushover_token", ""),
        "user": get("pushover_user", ""),
        "link": get("notify_link", ""),
        "last_sent": float(last_sent) if last_sent else None,
        "last_error": get("notify_last_error"),
    }


def save_settings(conn, body: dict) -> None:
    """Apply the Settings page's changes; blank keys leave the saved ones as they are."""
    for field, key in (("token", "pushover_token"), ("user", "pushover_user")):
        value = (body.get(field) or "").strip()
        if value:
            if not KEY_RE.match(value):
                raise ValueError(f"That doesn't look like a Pushover {'API token' if field == 'token' else 'user key'} "
                                 "(30 letters and numbers).")
            lr_db.set_state(conn, key, value)
    if "link" in body:
        link = (body["link"] or "").strip().rstrip("/")
        if link and not re.match(r"^https?://[^\s]{1,200}$", link):
            raise ValueError("The link should start with http:// or https://.")
        lr_db.set_state(conn, "notify_link", link)
    if "lead_days" in body:
        if body["lead_days"] not in LEAD_DAYS:
            raise ValueError("Choose how many days ahead to warn.")
        lr_db.set_state(conn, "notify_lead_days", str(body["lead_days"]))
    if "categories" in body:  # {category id: on}
        off = set(json.loads(lr_db.get_state(conn, "notify_off") or "[]"))
        switched_on = set(json.loads(lr_db.get_state(conn, "notify_on") or "[]"))
        for cid, on in body["categories"].items():
            if cid not in {c[0] for c in CATEGORIES}:
                raise ValueError(f"Unknown notification type {cid}.")
            if cid in DEFAULT_OFF:
                (switched_on.add if on else switched_on.discard)(cid)
            else:
                (off.discard if on else off.add)(cid)
        lr_db.set_state(conn, "notify_off", json.dumps(sorted(off)))
        lr_db.set_state(conn, "notify_on", json.dumps(sorted(switched_on)))
    if "enabled" in body:
        cfg = settings(conn)
        if body["enabled"] and not (cfg["token"] and cfg["user"]):
            raise ValueError("Save your user key and API token first.")
        lr_db.set_state(conn, "notify_enabled", "1" if body["enabled"] else "0")
        if not body["enabled"]:
            # start fresh next time, so problems still present then are reported
            lr_db.set_state(conn, "notify_active", "{}")


def send(token: str, user: str, title: str, message: str, priority: int = 0,
         url: str | None = None, url_title: str = "Open Litter Robot") -> None:
    fields = {"token": token, "user": user, "title": title[:250], "message": message[:MAX_MESSAGE],
              "priority": str(priority)}
    if url:
        fields |= {"url": url, "url_title": url_title}
    req = urllib.request.Request(API, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            errors = json.load(err).get("errors") or []
        except ValueError:
            errors = []
        raise PushoverError("Pushover refused it: " + ("; ".join(errors) or f"HTTP {err.code}")) from None
    except (urllib.error.URLError, TimeoutError) as err:
        raise PushoverError(f"Couldn't reach Pushover: {getattr(err, 'reason', err)}") from None


def compose(new: list[dict], link: str) -> dict:
    """One notification for everything new this sync."""
    if len(new) == 1:
        a = new[0]
        title, message = f"{a['subject']}", a["text"]
    else:
        title = f"{len(new)} new litter box alerts"
        lines = [a["text"] if a["group"] == "cats" else f"{a['subject']}: {a['text']}" for a in new]
        message = "\n".join(f"• {line}" for line in lines)
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE - 1] + "…"
    page = "/cats" if any(a["group"] == "cats" for a in new) else "/robots"
    return {"title": title, "message": message, "priority": max(a["priority"] for a in new),
            "url": link + page if link else None}


def notify(conn, alerts: list[dict], now: float | None = None) -> int:
    """Send what's new among `alerts`; returns how many were sent."""
    now = now or time.time()
    cfg = settings(conn)
    if not (cfg["enabled"] and cfg["token"] and cfg["user"]):
        return 0
    # a switched-off category counts as not active, so switching it back on reports what's there
    alerts = [a for a in alerts if a["category"] not in cfg["off"]]
    try:
        active = json.loads(lr_db.get_state(conn, "notify_active") or "{}")
    except ValueError:
        active = {}
    new = [a for a in alerts if a["key"] not in active]
    if new:
        try:
            send(cfg["token"], cfg["user"], **compose(new, cfg["link"]))
        except PushoverError as err:
            # leave them out of `active` so the next sync tries again
            LOG.warning("Notification not sent: %s", err)
            lr_db.set_state(conn, "notify_last_error", str(err))
            conn.commit()
            return 0
        LOG.info("Sent a notification for %d new alert%s", len(new), "" if len(new) == 1 else "s")
        lr_db.set_state(conn, "notify_last_sent", str(now))
        lr_db.set_state(conn, "notify_last_error", "")
    # remember what's active now; anything that cleared can be sent again if it returns
    lr_db.set_state(conn, "notify_active", json.dumps({a["key"]: active.get(a["key"], now) for a in alerts}))
    conn.commit()
    return len(new)


def visit_text(v, names: dict, unit: str, tz: ZoneInfo) -> str:
    pet = lr_db.first_pet(v["pet_ids"])
    parts = [f"{names.get(pet, 'A cat')} at {datetime.fromtimestamp(v['ts'], tz):%H:%M}"]
    if v["pet_weight"]:
        w = v["pet_weight"] * 0.453592 if unit == "kg" else v["pet_weight"]
        parts.append(f"{w:.2f} {unit}")
    if v["waste_type"] and v["waste_type"] != "Unknown":
        parts.append(v["waste_type"].lower())
    return " · ".join(parts)


def notify_visits(conn, serial: str, now: float | None = None) -> int:
    """Send the robot's visits since the last ones sent, as one notification; returns how
    many. Visits too light to be a cat are left out. Called by the collector once a
    clean cycle finishes and the activity log has been fetched."""
    import web  # the visit rules live with the web UI
    now = now or time.time()
    key = f"notify_visits_after:{serial}"
    # never reach back more than an hour, so switching this on doesn't send old visits
    after = max(float(lr_db.get_state(conn, key) or 0), now - 3600)
    rows = conn.execute("SELECT ts, pet_ids, pet_weight, waste_type FROM activities WHERE robot_serial = ? "
                        "AND type = 'PET_VISIT' AND ts > ? ORDER BY ts", (serial, after)).fetchall()
    if not rows:
        return 0
    lr_db.set_state(conn, key, str(rows[-1]["ts"]))
    conn.commit()
    visits = [r for r in rows if r["pet_weight"] is None or r["pet_weight"] >= web.IGNORE_UNDER_LB]
    cfg = settings(conn)
    if not visits or not (cfg["enabled"] and cfg["token"] and cfg["user"]) or "visit" in cfg["off"]:
        return 0
    robot = conn.execute("SELECT name, timezone FROM robots WHERE serial = ?", (serial,)).fetchone()
    tz = ZoneInfo((robot and robot["timezone"]) or "UTC")
    names = {r["pet_id"]: r["name"] for r in conn.execute("SELECT pet_id, name FROM pets")}
    unit = "lb" if lr_db.get_state(conn, "weight_unit") == "lb" else "kg"
    lines = [visit_text(v, names, unit, tz) for v in visits]
    title = f"{robot['name'] if robot else 'Litter-Robot'}: " + ("visit" if len(lines) == 1 else f"{len(lines)} visits")
    try:
        send(cfg["token"], cfg["user"], title, "\n".join(lines), url=cfg["link"] + "/events" if cfg["link"] else None)
    except PushoverError as err:  # a visit is only news for a while, so it isn't retried
        LOG.warning("Visit notification not sent: %s", err)
        lr_db.set_state(conn, "notify_last_error", str(err))
        conn.commit()
        return 0
    lr_db.set_state(conn, "notify_last_sent", str(now))
    lr_db.set_state(conn, "notify_last_error", "")
    conn.commit()
    return len(lines)


def after_sync() -> int:
    """Called by the collector after each sync."""
    import web  # the alert logic lives with the Cats page
    with web.app.app_context():
        conn = web.db()
        if not settings(conn)["enabled"]:
            return 0
        return notify(conn, web.current_alerts())
