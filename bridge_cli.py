"""Launch the full native Veusz editor or call its minimal bridge."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from veusz_bridge.client import Client, ROOT, SESSION_FILE
from veusz_bridge.desktop import desktop_context


def resolve_executable(executable=None):
    """Resolve CLI, environment, local config, then standard Windows locations."""
    configured = executable or os.environ.get("VEUSZ_EXE")
    config = ROOT / "veusz.local.json"
    if not configured and config.exists():
        settings = json.loads(config.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("veusz.local.json must be a JSON object")
        configured = settings.get("executable")
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("veusz.local.json must contain a nonempty executable string")
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise FileNotFoundError("Configured Veusz executable does not exist: " + str(path))
        return path.resolve()

    found = shutil.which("veusz.exe")
    if found:
        return Path(found).resolve()
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            suffix = "Programs/Veusz/veusz.exe" if variable == "LOCALAPPDATA" else "Veusz/veusz.exe"
            path = Path(base) / suffix
            if path.is_file():
                return path.resolve()
    raise FileNotFoundError(
        "Veusz executable not found. Set VEUSZ_EXE, create veusz.local.json "
        "from examples/veusz.local.example.json, or pass launch --exe PATH.")


def process_running(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied is not evidence of exit.
    try:
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)


def window_readiness_error(state, expected_pid=None):
    """Return why a bridge session is not attached to a real visible desktop window."""
    pid = state.get("pid")
    if expected_pid is not None and pid != expected_pid:
        return "bridge PID does not match the launched Veusz process"
    handle = state.get("window_handle")
    if not isinstance(handle, int) or handle <= 0:
        return "Veusz has no native window handle"
    if not state.get("window_native_exists"):
        return "Veusz native window handle is not valid"
    if state.get("window_native_pid") != pid:
        return "native window belongs to a different process"
    if state.get("desktop_is_interactive") is not True:
        return "Veusz is not verified on the user's input desktop (thread=%s, input=%s)" % (
            state.get("thread_desktop"), state.get("input_desktop"))
    if not state.get("window_visible") or not state.get("window_native_visible"):
        return "Veusz native window is hidden"
    if state.get("window_native_minimized"):
        return "Veusz window is minimized"
    if not state.get("window_native_on_screen"):
        return "Veusz window is outside the visible desktop bounds"
    return None


def request_window_foreground(state):
    """Restore and request foreground for an existing Veusz window."""
    if os.name != "nt":
        raise RuntimeError("Visible desktop verification is only implemented on Windows")
    import ctypes
    from ctypes import wintypes

    handle = state.get("window_handle")
    if not isinstance(handle, int) or handle <= 0:
        raise RuntimeError("Veusz is connected but has no native window handle")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = wintypes.HWND(handle)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    if not user32.IsWindow(hwnd):
        raise RuntimeError("Veusz native window handle is no longer valid")
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 1)  # SW_RESTORE / SW_SHOWNORMAL
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)


def launch(executable=None, session_file=None, label=None):
    context = desktop_context()
    if not context["desktop_is_interactive"]:
        raise RuntimeError(
            "Launcher is not on the user's interactive desktop (thread=%s, input=%s). "
            "Run start_veusz.cmd from the desktop, or use approved unsandboxed desktop execution. %s" % (
                context["thread_desktop"], context["input_desktop"],
                context["desktop_error"] or ""))
    session_file = Path(session_file or os.environ.get("VEUSZ_SESSION_FILE", SESSION_FILE)).resolve()
    session_file.parent.mkdir(parents=True, exist_ok=True)
    lock = session_file.with_suffix(".launch.lock")
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("Launch is already in progress; inspect the session before retrying")
    try:
        os.close(descriptor)
        return _launch(executable, session_file, label)
    finally:
        lock.unlink(missing_ok=True)


def _launch(executable, session_file, label=None):
    pending_file = session_file.with_suffix(".starting.json")
    if pending_file.exists():
        pending = json.loads(pending_file.read_text(encoding="utf-8"))
        if process_running(pending["pid"]) and not session_file.exists():
            raise RuntimeError("A previously started Veusz is still initializing; will not launch another")
    if session_file.exists():
        previous = json.loads(session_file.read_text(encoding="utf-8"))
        try:
            current = Client(session_file).state()
        except OSError:
            if process_running(previous["pid"]):
                raise RuntimeError("Existing Veusz process is alive but unreachable; will not open another window")
        else:
            if current.get("desktop_is_interactive") is not True:
                raise RuntimeError(window_readiness_error(current, previous["pid"]))
            request_window_foreground(current)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                current = Client(session_file).state()
                problem = window_readiness_error(current, current.get("pid"))
                if problem is None:
                    pending_file.unlink(missing_ok=True)
                    return {"reused": True, "state": current}
                time.sleep(0.1)
            raise RuntimeError("Veusz is connected but desktop visibility was not confirmed: " + problem)
    executable = resolve_executable(executable)
    if session_file.exists():
        session_file.unlink()
    error_file = session_file.parent / "plugin_error.txt"
    if error_file.exists():
        error_file.unlink()
    env = os.environ.copy()
    # The GUI owns its Qt/Python distribution; do not inherit another runtime's
    # search paths into this frozen application.
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    env.update(VEUSZ_BRIDGE_ROOT=str(ROOT), VEUSZ_BRIDGE_SESSION_FILE=str(session_file),
               VEUSZ_BRIDGE_TOKEN=secrets.token_hex(32), VEUSZ_BRIDGE_LABEL=label or "")
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
    with (session_file.parent / "veusz.log").open("ab") as log:
        process = subprocess.Popen([str(executable), "--veusz-plugin", str(ROOT / "plugin_entry.py")],
                                   cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log,
                                   startupinfo=startupinfo,
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    pending_file.write_text(json.dumps({"pid": process.pid, "executable": str(executable)}), encoding="utf-8")
    deadline = time.monotonic() + 25
    foreground_requested = False
    while time.monotonic() < deadline:
        if error_file.exists():
            raise RuntimeError(error_file.read_text(encoding="utf-8"))
        if process.poll() is not None:
            raise RuntimeError("Veusz exited: %s; see runtime/veusz.log" % process.returncode)
        if session_file.exists():
            client = Client(session_file)
            try:
                state = client.state()
                if not foreground_requested:
                    foreground_requested = True
                    request_window_foreground(state)
                    continue
                problem = window_readiness_error(state, process.pid)
                if problem is None:
                    pending_file.unlink(missing_ok=True)
                    return {"reused": False, "state": state}
                last_window_problem = problem
            except (OSError, RuntimeError) as exc:
                last_window_problem = str(exc)
        time.sleep(0.15)
    detail = locals().get("last_window_problem")
    if detail:
        raise TimeoutError("Bridge connected, but Veusz desktop window was not confirmed: " + detail)
    raise TimeoutError("Bridge not ready; check Veusz startup dialogs and runtime/veusz.log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("launch")
    start.add_argument("--exe", help="Veusz executable; alternatively set VEUSZ_EXE or veusz.local.json")
    start.add_argument("--demo", action="store_true", help="Create demo only when absent")
    start.add_argument("--new-window", action="store_true", help="Create a separate native editor instead of reusing one")
    start.add_argument("--name", default="Figure", help="Label for a new independent window")
    start.add_argument("--start-host", action="store_true", help="Start the independent real-desktop launch host only")
    call = sub.add_parser("call")
    call.add_argument("method", choices=("state", "demo", "get", "describe", "set", "undo", "snapshot", "export", "import_data", "import_file", "file_formats", "widget_catalog", "set_data", "widget_action", "window_view", "plot", "add_widgets", "save", "open", "checkpoint", "operation", "close"))
    call.add_argument("--window-id", help="Stable editor identity from veusz_windows")
    payload = call.add_mutually_exclusive_group()
    payload.add_argument("--params", default="{}")
    payload.add_argument("--params-file", type=Path, help="UTF-8 JSON file; avoids shell quoting")
    args = parser.parse_args()
    if args.command == "launch":
        from veusz_bridge.desktop_host import ensure_host
        host = ensure_host(allow_start=True)
        if args.start_host:
            print(json.dumps(host, ensure_ascii=True))
            return
        if args.new_window:
            from veusz_bridge.windows import create_window
            result = create_window(args.name, args.exe)
        else:
            result = launch(args.exe)
        if args.demo and not any(w["path"] == "/codex_demo" for w in result["state"]["widgets"]):
            from veusz_bridge.windows import select_session
            client = Client(select_session(result["state"]["bridge_id"]))
            result["state"] = client.call("demo", state=client.state())
    else:
        from veusz_bridge.windows import select_session, has_multiple_sessions
        client = Client(select_session(args.window_id)) if args.window_id or has_multiple_sessions() else Client()
        from veusz_bridge.protocol import WRITE_METHODS
        state = client.state() if args.method in WRITE_METHODS else None
        params = (json.loads(args.params_file.read_text(encoding="utf-8-sig"))
                  if args.params_file else json.loads(args.params))
        result = client.call(args.method, params, state=state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
