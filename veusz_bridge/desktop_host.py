"""User-desktop launch host, independent of MCP clients' kill-on-close process jobs.

Started by the visible desktop launcher, never used to escape an isolated desktop.
Only authenticated local requests to launch project-owned Veusz sessions are accepted.
"""

import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import time

from .client import ROOT
from .desktop import desktop_context
from .windows import default_session, window_directory


def host_file():
    return default_session().parent / "desktop-host.json"


def request_host(request):
    info = json.loads(host_file().read_text(encoding="utf-8"))
    if info["host"] != "127.0.0.1":
        raise RuntimeError("Desktop host must use loopback")
    request = {**request, "token": info["token"]}
    with socket.create_connection((info["host"], info["port"]), timeout=3) as connection:
        connection.settimeout(40)
        connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(2 * 1024 * 1024))
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Desktop host request failed"))
    return response["result"]


def ensure_host(allow_start=False):
    try:
        return request_host({"method": "ping"})
    except (OSError, ValueError, RuntimeError, KeyError):
        if not allow_start:
            raise RuntimeError("DESKTOP_HOST_REQUIRED: run start_veusz.cmd --start-host on the real desktop using approved desktop execution")
    context = desktop_context()
    if not context["desktop_is_interactive"]:
        raise RuntimeError("DESKTOP_HOST_REQUIRED: launch the desktop host from the user's input desktop")
    info_path = host_file()
    info_path.parent.mkdir(parents=True, exist_ok=True)
    lock = info_path.with_suffix(".launch.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("Desktop host launch already in progress; retry status shortly")
    try:
        os.close(descriptor)
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        launch_id = secrets.token_hex(16)
        environment = dict(os.environ, VEUSZ_DESKTOP_HOST_ID=launch_id)
        with info_path.with_suffix(".log").open("ab") as log:
            process = subprocess.Popen(
                [str(pythonw if pythonw.exists() else python), "-m", "veusz_bridge.desktop_host"],
                cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Desktop host exited; inspect " + str(info_path.with_suffix(".log")))
            try:
                result = request_host({"method": "ping"})
                if result.get("host_id") == launch_id:
                    return result
            except (OSError, ValueError, RuntimeError, KeyError):
                pass
            time.sleep(0.1)
        raise TimeoutError("Desktop host did not become ready")
    finally:
        lock.unlink(missing_ok=True)


def launch_window(session_file, executable, label):
    ensure_host()
    return request_host({"method": "launch", "session_file": str(session_file),
                         "executable": str(executable) if executable is not None else None, "label": label})


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            line = self.rfile.readline(65537)
            if len(line) > 65536 or not line.endswith(b"\n"):
                raise ValueError("Invalid desktop-host request")
            request = json.loads(line)
            token = request.get("token")
            if not isinstance(token, str) or not secrets.compare_digest(token, self.server.token):
                raise ValueError("Desktop-host authentication failed")
            if request.get("method") == "ping":
                result = {"pid": os.getpid(), "host_id": self.server.host_id,
                          "desktop": desktop_context(), "ready": True}
            elif request.get("method") == "launch":
                target = Path(request["session_file"]).resolve()
                if self.server.directory not in target.parents or target.name != "session.json":
                    raise ValueError("Only managed project window sessions may be launched")
                label = request.get("label", "Figure")
                if not isinstance(label, str) or not 1 <= len(label) <= 80:
                    raise ValueError("Invalid window label")
                from bridge_cli import launch
                result = launch(request.get("executable"), target, label=label)
            else:
                raise ValueError("Unknown desktop-host request")
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            self.wfile.write(json.dumps(response, ensure_ascii=True).encode("utf-8") + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


def main():
    if not desktop_context()["desktop_is_interactive"]:
        raise RuntimeError("Desktop host requires the real input desktop")
    with Server(("127.0.0.1", 0), Handler) as server:
        server.token = secrets.token_hex(32)
        server.host_id = os.environ.get("VEUSZ_DESKTOP_HOST_ID", secrets.token_hex(16))
        server.directory = window_directory().resolve()
        path = host_file()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"host": "127.0.0.1", "port": server.server_address[1],
                                          "pid": os.getpid(), "token": server.token}), encoding="utf-8")
        os.replace(temporary, path)
        server.serve_forever()


if __name__ == "__main__":
    main()
