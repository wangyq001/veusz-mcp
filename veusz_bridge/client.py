"""Dependency-free client, independent of Veusz's bundled Python and Qt."""

import json
import os
from pathlib import Path
import socket
import uuid

from .protocol import MAX_MESSAGE

ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = ROOT / "runtime" / "v1" / "session.json"


class RemoteError(RuntimeError):
    def __init__(self, error):
        super().__init__(error["code"] + ": " + error["message"])
        self.code = error["code"]


class Client:
    def __init__(self, session_file=None):
        self.session_file = Path(session_file or os.environ.get("VEUSZ_SESSION_FILE", SESSION_FILE))
        self.session = json.loads(self.session_file.read_text(encoding="utf-8"))
        if self.session["host"] != "127.0.0.1":
            raise ValueError("Only a local bridge is supported")

    def make_request(self, method, params=None, state=None, request_id=None, timeout_ms=10000):
        request = {"id": request_id or str(uuid.uuid4()), "token": self.session["token"],
                   "method": method, "params": params or {}, "timeout_ms": timeout_ms}
        if state is not None:
            request.update(document_id=state["document_id"], expected_revision=state["revision"])
        return request

    def send(self, request):
        data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > MAX_MESSAGE:
            raise ValueError("Request too large")
        with socket.create_connection((self.session["host"], self.session["port"]), timeout=3) as sock:
            sock.settimeout(request.get("timeout_ms", 10000) / 1000 + 5)
            sock.sendall(data)
            with sock.makefile("rb") as stream:
                line = stream.readline(MAX_MESSAGE + 1)
        if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
            raise RuntimeError("Invalid bridge response")
        response = json.loads(line)
        if not response["ok"]:
            raise RemoteError(response["error"])
        return response["result"]

    def call(self, method, params=None, state=None, request_id=None, timeout_ms=10000):
        return self.send(self.make_request(method, params, state, request_id, timeout_ms))

    def state(self):
        return self.call("state")
