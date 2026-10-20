"""Read-only diagnostics. Does not launch, close, restart or edit a GUI."""
import importlib.metadata
import json
from pathlib import Path
import sys

from veusz_bridge.client import Client, SESSION_FILE
from veusz_bridge.desktop import desktop_context
from bridge_cli import window_readiness_error


def main():
    result = {"python": sys.executable, "session_file": str(SESSION_FILE),
              "launcher_desktop": desktop_context()}
    try:
        result["mcp_sdk"] = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        result["mcp_sdk"] = "missing"
    try:
        state = Client().state()
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
