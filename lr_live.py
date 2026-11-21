"""Live camera view for the web UI.

The browser is the WebRTC peer; this only relays signaling (SDP offer/answer and
ICE candidates) between it and the camera through pylitterbot's
CameraSignalingRelay, so video goes straight from the camera to the browser.

Flask handlers are synchronous, so the Whisker account and every relay live on
one asyncio loop in a background thread, reached through run().
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from typing import Any

from pylitterbot import Account
from pylitterbot.camera import CameraSignalingRelay
from pylitterbot.exceptions import CameraStreamException
from pylitterbot.robot.litterrobot5 import LitterRobot5
from pylitterbot.utils import first_value

LOG = logging.getLogger("lr_live")
ACCESS_LOG = logging.getLogger("lr_access")  # access.log: who watched, for how long

# why the page stopped a view (sent with its close request); anything else is logged as "closed"
END_REASONS = {"stopped": "stopped", "limit": "reached the 10-minute limit", "hidden": "page hidden",
               "left": "page closed", "failed": "connection lost", "error": "couldn't connect"}

IDLE_TIMEOUT_S = 45  # the page polls every second while it's open
MAX_SESSIONS = 4
FALLBACK_STUN = "stun:stun.l.google.com:19302"


class LiveError(Exception):
    """A problem to show to the viewer as-is."""


class _Session:
    def __init__(self, serial: str, relay: CameraSignalingRelay, who: str, robot: str) -> None:
        self.serial = serial
        self.relay = relay
        self.candidates: list[dict[str, Any]] = []  # from the camera, for the browser to poll
        self.seen = self.started = time.monotonic()
        self.who, self.robot = who, robot


_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_account: Account | None = None
_sessions: dict[str, _Session] = {}


def run(coro, timeout: float = 60) -> Any:
    """Run a coroutine on the live loop from a Flask thread and wait for it."""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="lr-live", daemon=True).start()
            asyncio.run_coroutine_threadsafe(_reap(), _loop)
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


async def _robot(serial: str) -> LitterRobot5:
    global _account
    if _account is None:
        if not (os.environ.get("LR_USERNAME") and os.environ.get("LR_PASSWORD")):
            raise LiveError("LR_USERNAME and LR_PASSWORD aren't set")
        account = Account()
        try:
            await account.connect(username=os.environ["LR_USERNAME"], password=os.environ["LR_PASSWORD"],
                                  load_robots=True)
        except Exception as err:
            await account.disconnect()
            LOG.warning("Live view: Whisker login failed: %s", err)
            raise LiveError("Couldn't log in to Whisker") from err
        _account = account
    robot = next((r for r in _account.robots if r.serial == serial), None)
    if not isinstance(robot, LitterRobot5) or not robot.has_camera:
        raise LiveError("This robot has no camera")
    return robot


def _ice_servers(turn_servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whisker's TURN credentials in the browser's RTCIceServer shape."""
    servers = []
    for cred in turn_servers:
        urls = first_value(cred, ("turnUrl", "urls", "uris")) or []
        if urls:
            servers.append({"urls": urls, "username": cred.get("username", ""),
                            "credential": first_value(cred, ("password", "credential"), default="")})
        if cred.get("stunUrl"):
            servers.append({"urls": cred["stunUrl"]})
    if not any(not s.get("username") for s in servers):
        servers.append({"urls": FALLBACK_STUN})
    return servers


async def start(serial: str, who: str = "someone") -> dict[str, Any]:
    """Wake the camera and return what the browser needs to build its offer. `who` names the
    viewer in the access log."""
    robot = await _robot(serial)
    if len(_sessions) >= MAX_SESSIONS:
        raise LiveError("Too many live views are open; close one and try again")
    status = await robot.get_camera_status()
    if status and status != "ONLINE":
        raise LiveError(f"The camera is {status.lower()}")
    client = robot.get_camera_client()
    try:
        # wakes the camera while the browser builds its offer; the relay opens its own
        session = await client.generate_session()
    except CameraStreamException as err:
        LOG.warning("Live view: %s", err)
        raise LiveError("Whisker didn't start a camera session") from err
    sid = uuid.uuid4().hex
    _sessions[sid] = _Session(serial, CameraSignalingRelay(client), who, robot.name)
    ACCESS_LOG.info("Live view started: %s on %s", who, robot.name)
    return {"id": sid, "ice_servers": _ice_servers(session.turn_servers), "view": await robot.get_camera_view()}


def _session(sid: str) -> _Session:
    s = _sessions.get(sid)
    if s is None:
        raise LiveError("This live view has ended")
    s.seen = time.monotonic()
    return s


async def offer(sid: str, sdp: str) -> str:
    """Send the browser's offer; returns the camera's answer (usually 1-3 s, up to ~25 s)."""
    s = _session(sid)
    answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    try:
        await s.relay.start(sdp, lambda a: answer.done() or answer.set_result(a), s.candidates.append)
    except CameraStreamException as err:
        await close(sid, "the camera didn't answer")
        LOG.warning("Live view: %s", err)
        raise LiveError("The camera didn't answer; it may be asleep or offline") from err
    return await answer


async def add_candidate(sid: str, candidate: dict[str, Any]) -> None:
    await _session(sid).relay.send_candidate(candidate)


async def candidates(sid: str, after: int) -> list[dict[str, Any]]:
    """The camera's ICE candidates past index after; polling also keeps the session alive."""
    return _session(sid).candidates[after:]


async def close(sid: str, reason: str = "closed") -> None:
    s = _sessions.pop(sid, None)
    if s is not None:
        secs = round(time.monotonic() - s.started)
        ACCESS_LOG.info("Live view ended: %s on %s after %s (%s)", s.who, s.robot,
                        f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s", reason)
        await s.relay.close()


async def set_view(serial: str, view: str) -> bool:
    return await (await _robot(serial)).set_camera_view(view)


async def _reap() -> None:
    """Close relays whose page went away without saying so."""
    while True:
        await asyncio.sleep(15)
        now = time.monotonic()
        for sid in [k for k, s in _sessions.items() if now - s.seen > IDLE_TIMEOUT_S]:
            await close(sid, "the page stopped responding")
