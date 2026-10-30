"""Discover and route independent Veusz editors without global active-window state."""

import json
import os
from pathlib import Path
import uuid
import math

from .client import Client, SESSION_FILE
from .protocol import BridgeError


def default_session():
    return Path(os.environ.get("VEUSZ_SESSION_FILE", SESSION_FILE)).resolve()


def window_directory():
    return default_session().parent / "windows"


def has_multiple_sessions():
    return any(window_directory().glob("*/session.json"))


def session_metadata():
    result = []
    for path in [default_session(), *sorted(window_directory().glob("*/session.json"))]:
        if not path.is_file():
            continue
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            result.append({"path": path, "info": info, "window_id": info.get("bridge_id") or
                           str(uuid.uuid5(uuid.NAMESPACE_URL, str(path)))})
        except (OSError, ValueError):
            continue
    return result


def process_alive(info):
    if os.name == "nt" and info.get("pid"):
        from bridge_cli import process_running
        return process_running(info["pid"])
    return True


def records():
    paths = [default_session(), *sorted(window_directory().glob("*/session.json"))]
    result, seen = [], set()
    for path in paths:
        if not path.is_file():
            continue
        info = {}
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            if os.name == "nt" and info.get("pid"):
                from bridge_cli import process_running
                if not process_running(info["pid"]):
                    raise ConnectionError("Veusz process has exited")
            state = Client(path).call("state", timeout_ms=2000)
            if info.get("bridge_id") and state.get("bridge_id") != info["bridge_id"]:
                raise BridgeError("WINDOW_CHANGED", "Bridge identity no longer matches its session")
            identity = state.get("bridge_id") or str(uuid.uuid5(uuid.NAMESPACE_URL, str(path)))
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"path": path, "state": state, "window_id": identity, "available": True})
        except (OSError, ValueError, RuntimeError, KeyError, BridgeError) as exc:
            identity = info.get("bridge_id") or str(uuid.uuid5(uuid.NAMESPACE_URL, str(path)))
            if identity not in seen:
                seen.add(identity)
                result.append({"path": path, "window_id": identity, "available": False,
                               "state": {"pid": info.get("pid"), "window_label": info.get("window_label", "")},
                               "error": str(exc)})
    return result


def list_windows():
    result = []
    for record in records():
        state = record["state"]
        fields = ("pid", "document_id", "revision", "filename", "modified", "window_label",
                  "window_handle", "window_visible", "window_native_minimized", "window_foreground",
                  "thread_desktop", "input_desktop", "desktop_is_interactive")
        result.append({"window_id": record["window_id"], "available": record["available"],
                       **{key: state.get(key) for key in fields},
                       **({"error": record["error"]} if "error" in record else {})})
    return {"windows": result, "target_required_when_multiple": True}


def select_session(window_id=None, document_id=None):
    if window_id is not None:
        # Route by authenticated metadata; never wait for another window's Qt event loop.
        matching = [r for r in session_metadata() if r["window_id"] == window_id]
        if not matching or not process_alive(matching[0]["info"]):
            raise BridgeError("WINDOW_UNAVAILABLE", "Window is closed or unavailable; inspect veusz_windows")
        return matching[0]["path"]
    available = records()
    live = [r for r in available if r["available"]]
    if document_id is not None:
        matching = [r for r in live if r["state"].get("document_id") == document_id]
        if len(matching) == 1:
            return matching[0]["path"]
        if len(live) == 1:
            # Retain native DOCUMENT_CHANGED handling for stale document identities.
            return live[0]["path"]
        raise BridgeError("DOCUMENT_CHANGED", "Document is no longer uniquely available; inspect veusz_windows")
    if len(live) == 1:
        return live[0]["path"]
    if len(live) > 1:
        raise BridgeError("AMBIGUOUS_WINDOW", "Multiple editors are open; pass window_id from veusz_windows")
    raise BridgeError("WINDOW_UNAVAILABLE", "No connected Veusz editor; launch a visible window")


def create_window(name="Figure", executable=None, request_id=None):
    from .desktop_host import launch_window

    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise BridgeError("BAD_REQUEST", "Window name must contain 1..80 characters")
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
        raise BridgeError("BAD_REQUEST", "request_id must contain 1..128 characters")
    key = uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex if request_id else uuid.uuid4().hex
    target = window_directory() / key / "session.json"
    if request_id and target.exists():
        try:
            Client(target).state()
        except (OSError, ValueError, RuntimeError) as exc:
            raise BridgeError("WINDOW_UNAVAILABLE", "The requested window was closed or is unreachable; use a new request_id to create another") from exc
    state = launch_window(target, executable, name)["state"]
    return {"window_id": state["bridge_id"], "state": state}


def arrange_windows(window_ids, columns=2):
    if (not isinstance(window_ids, list) or not 1 <= len(window_ids) <= 16 or
            not all(isinstance(v, str) for v in window_ids) or len(set(window_ids)) != len(window_ids) or
            type(columns) is not int or not 1 <= columns <= len(window_ids)):
        raise BridgeError("BAD_REQUEST", "Provide 1..16 unique window_ids and a valid column count")
    targets = []
    for identity in window_ids:
        client = Client(select_session(identity))
        state = client.state()
        if "window_screen_geometry" not in state:
            raise BridgeError("BRIDGE_UPDATE_REQUIRED", "This window uses an older plugin; save it before manually restarting")
        targets.append((identity, client, state))
    x, y, width, height = targets[0][2]["window_screen_geometry"]
    rows = math.ceil(len(targets) / columns)
    if width // columns < 240 or height // rows < 200:
        raise BridgeError("BAD_REQUEST", "Too many windows for the current screen work area")
    result = []
    for index, (identity, client, state) in enumerate(targets):
        col, row = index % columns, index // columns
        bounds = [x + width * col // columns, y + height * row // rows,
                  width * (col + 1) // columns - width * col // columns,
                  height * (row + 1) // rows - height * row // rows]
        try:
            updated = client.call("window_view", {"geometry": bounds, "fit_page": True}, state=state)
            result.append({"window_id": identity, "passed": True, "geometry": bounds,
                           "revision": updated["revision"]})
        except (OSError, ValueError, RuntimeError) as exc:
            result.append({"window_id": identity, "passed": False, "error": str(exc)})
    return {"windows": result, "complete": all(item["passed"] for item in result)}
