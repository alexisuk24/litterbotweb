"""Robot IQ: forecasts of when a robot's waste drawer will be full and its litter hopper
will need refilling, learned from what the collector records at each sync.

Drawer: the robot reports a fill level (%) and its clean-cycle odometer at every sync.
Since the last emptying, the level rises by roughly the same amount per clean cycle; that
rate times the robot's recent cycles per day gives a date. "Full" is the level the robot
was at when it last reported the drawer full (some flag full well below 100%).

Hopper: the LR5 reports no hopper level, only Ready / Litter low / Empty, and logs each
dispense. Once a refill has been seen run down to Litter low, the number of dispenses that
took is the hopper's capacity; dispenses since the last refill and the recent dispense rate
then give a date. Until then only the robot's own Litter low / Empty status is known."""
from __future__ import annotations

import json
import statistics
import time

EMPTIED_DROP = 20     # a fall in drawer level at least this big means it was emptied
FULL_MATCH_S = 3 * 3600  # a snapshot this close to a DRAWER_FULL event shows the "full" level
LOW_STATES = ("LITTER_LOW", "EMPTY")


def _slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of y over x."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if not sxx:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / sxx


def cycles_per_day(conn, serial: str, now: float, days: int = 7) -> float | None:
    first = conn.execute("SELECT MIN(ts) AS ts FROM activities WHERE robot_serial = ?", (serial,)).fetchone()["ts"]
    if not first:
        return None
    span = min(days * 86400, now - first)
    if span < 86400:
        return None
    n = conn.execute("SELECT COUNT(*) AS n FROM activities WHERE robot_serial = ? AND type = 'CYCLE_COMPLETED' "
                     "AND ts >= ?", (serial, now - span)).fetchone()["n"]
    return n / (span / 86400)


def full_level(conn, serial: str, snaps, info: dict | None) -> float:
    """The drawer level this robot calls full: what it read at its DRAWER_FULL events."""
    levels = []
    for r in conn.execute("SELECT ts FROM activities WHERE robot_serial = ? AND type = 'DRAWER_FULL'", (serial,)):
        near = [s for s in snaps if abs(s["ts"] - r["ts"]) <= FULL_MATCH_S]
        if near:
            levels.append(min(near, key=lambda s: abs(s["ts"] - r["ts"]))["waste_drawer_level"])
    if info and "Waste drawer full" in (info.get("faults") or []) and info.get("drawer_level") is not None:
        levels.append(info["drawer_level"])
    levels = [x for x in levels if x and x >= 40]
    return min(100.0, statistics.median(levels)) if levels else 100.0


def drawer_forecast(conn, serial: str, info: dict | None, now: float | None = None) -> dict | None:
    now = now or time.time()
    snaps = conn.execute(
        "SELECT ts, waste_drawer_level, cycle_count FROM robot_snapshots WHERE robot_serial = ? "
        "AND waste_drawer_level IS NOT NULL AND ts >= ? ORDER BY ts", (serial, now - 60 * 86400)).fetchall()
    if not snaps:
        return None
    start = 0
    for i in range(1, len(snaps)):
        if snaps[i]["waste_drawer_level"] <= snaps[i - 1]["waste_drawer_level"] - EMPTIED_DROP:
            start = i
    period = snaps[start:]
    level = period[-1]["waste_drawer_level"]
    full = full_level(conn, serial, snaps, info)
    out = {"level": level, "full_level": full, "since": period[0]["ts"] if start else None}
    if level >= full or (info and "Waste drawer full" in (info.get("faults") or [])):
        return out | {"state": "full"}
    pts = [(s["cycle_count"], s["waste_drawer_level"]) for s in period if s["cycle_count"] is not None]
    rate = None
    if len(pts) >= 3 and pts[-1][0] - pts[0][0] >= 10 and pts[-1][1] - pts[0][1] >= 5:
        rate = _slope(pts)
    cpd = cycles_per_day(conn, serial, now)
    out |= {"per_cycle": round(rate, 3) if rate else None, "cycles_per_day": round(cpd, 1) if cpd else None}
    if not rate or rate <= 0 or not cpd:
        return out | {"state": "learning"}
    days = (full - level) / rate / cpd
    return out | {"state": "ok", "days": round(days, 1), "eta": now + days * 86400}


def hopper_forecast(conn, serial: str, raw_status: str | None, now: float | None = None) -> dict | None:
    now = now or time.time()
    if not raw_status or raw_status == "DISABLED":
        return None
    snaps = conn.execute(
        "SELECT ts, hopper_status FROM robot_snapshots WHERE robot_serial = ? AND hopper_status IS NOT NULL "
        "ORDER BY ts", (serial,)).fetchall()
    dispenses = [r["ts"] for r in conn.execute(
        "SELECT ts FROM activities WHERE robot_serial = ? AND type = 'HOPPER_DISPENSED' ORDER BY ts", (serial,))]
    # refills (low/empty -> ready) and run-downs (ready -> low/empty) seen in the snapshots
    refills, lows = [], []
    for a, b in zip(snaps, snaps[1:]):
        if a["hopper_status"] in LOW_STATES and b["hopper_status"] == "READY":
            refills.append(b["ts"])
        elif a["hopper_status"] == "READY" and b["hopper_status"] in LOW_STATES:
            lows.append(b["ts"])
    runs = []
    for r in refills:
        nxt = next((t for t in lows if t > r), None)
        if nxt:
            runs.append(sum(1 for d in dispenses if r < d <= nxt))
    capacity = statistics.median(runs) if runs else None
    last_refill = refills[-1] if refills else None
    used = sum(1 for d in dispenses if last_refill and d > last_refill) if last_refill else None
    span = min(30 * 86400, now - dispenses[0]) if dispenses else 0
    rate = sum(1 for d in dispenses if d >= now - span) / (span / 86400) if span >= 3 * 86400 else None
    out = {"status": raw_status, "capacity": capacity, "since_refill": used, "last_refill": last_refill,
           "per_day": round(rate, 2) if rate else None}
    if raw_status == "EMPTY":
        return out | {"state": "empty"}
    if raw_status == "LITTER_LOW":
        return out | {"state": "low"}
    if raw_status != "READY":
        return out | {"state": "fault"}
    if not (capacity and used is not None and rate):
        return out | {"state": "learning"}
    days = max(0.0, (capacity - used) / rate)
    return out | {"state": "ok", "days": round(days, 1), "eta": now + days * 86400}


def forecasts(conn, serial: str, now: float | None = None) -> dict:
    r = conn.execute("SELECT info, raw FROM robots WHERE serial = ?", (serial,)).fetchone()
    info = json.loads(r["info"]) if r and r["info"] else None
    raw = json.loads(r["raw"]) if r and r["raw"] else {}
    status = ((raw.get("state") or {}).get("hopperStatusIndicator") or {}).get("value")
    if (raw.get("state") or {}).get("isHopperInstalled") is False:
        status = None
    return {"drawer": drawer_forecast(conn, serial, info, now),
            "hopper": hopper_forecast(conn, serial, status, now)}
