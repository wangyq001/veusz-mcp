"""Read-only diagnostics. Does not launch, close, restart or edit a GUI."""
import importlib.metadata
import argparse
import json
from pathlib import Path
import sys

from veusz_bridge.client import Client, SESSION_FILE
from veusz_bridge.desktop import desktop_context
from bridge_cli import window_readiness_error
from veusz_bridge.windows import list_windows, select_session, has_multiple_sessions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-id", help="Select an editor when multiple windows exist")
    args = parser.parse_args()
    result = {"python": sys.executable, "session_file": str(SESSION_FILE),
              "launcher_desktop": desktop_context()}
    try:
        result["mcp_sdk"] = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        result["mcp_sdk"] = "missing"
    if has_multiple_sessions():
        result["windows"] = list_windows()["windows"]
        live = [w for w in result["windows"] if w["available"]]
        result["connected_windows"] = len(live)
        if len(live) > 1 and not args.window_id:
            result["action"] = "Multiple editors are connected. Use --window-id or veusz_status(window_id=...) to inspect one."
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
    try:
        client = Client(select_session(args.window_id)) if args.window_id or has_multiple_sessions() else Client()
        state = client.state()
        result["connection"] = {k: state.get(k) for k in
                                ("pid", "bridge_version", "document_id", "revision", "modified",
                                 "filename", "modal", "modal_title", "window_visible",
                                 "thread_desktop", "input_desktop", "desktop_is_interactive",
                                 "window_native_visible", "window_native_on_screen", "window_foreground")}
        result["visibility_error"] = window_readiness_error(state)
        result["visible_connection_ready"] = result["visibility_error"] is None
    except Exception as exc:
        result["connection_error"] = "%s: %s" % (type(exc).__name__, exc)
        result["action"] = "Start the GUI with start_veusz.cmd on the visible desktop; do not kill an existing window"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
