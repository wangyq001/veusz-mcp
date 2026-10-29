"""Bounded local JSON transport. This module deliberately has no Qt dependency."""

import hashlib
import hmac
import json
import socketserver
import threading
import time
from collections import OrderedDict
from pathlib import Path

MAX_MESSAGE = 2 * 1024 * 1024
WRITE_METHODS = frozenset(("set", "demo", "undo", "import_data", "plot", "add_widgets", "set_data", "widget_action",
                           "save", "open", "checkpoint", "close", "export", "snapshot"))


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def failure(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


class Job:
    def __init__(self, request, fingerprint, deadline):
        self.request = request
        self.fingerprint = fingerprint
        self.deadline = deadline
        self.state = "queued"
        self.result = None
        self.event = threading.Event()
        self.lock = threading.Lock()


class Broker:
    """Deduplicate requests across reconnects; Qt owns execution, not this class."""

    def __init__(self, token, dispatch, capacity=256, journal=None):
        self.token = token
        self.dispatch = dispatch
        self.capacity = capacity
        self.jobs = OrderedDict()
        self.lock = threading.Lock()
        self.journal = Path(journal) if journal else None
        self.seen_writes = set()
        if self.journal:
            self.journal.mkdir(parents=True, exist_ok=True)

    def record_path(self, rid):
        return self.journal / (hashlib.sha256(rid.encode()).hexdigest() + ".json")

    def record(self, job):
        if self.journal and job.request["method"] in WRITE_METHODS:
            path = self.record_path(job.request["id"])
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"fingerprint": job.fingerprint, "state": job.state,
                           "result": job.result}, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
            temporary.replace(path)

    def status(self, rid):
        with self.lock:
            job = self.jobs.get(rid)
            if job:
                with job.lock:
                    return {"state": job.state, "response": job.result}
            if self.journal and self.record_path(rid).exists():
                return json.loads(self.record_path(rid).read_text(encoding="utf-8"))
        raise BridgeError("OPERATION_UNKNOWN", "No operation with this id in the current bridge session")

    def submit(self, request):
        if not isinstance(request, dict):
            raise BridgeError("BAD_REQUEST", "Expected a JSON object")
        token = request.get("token", "")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode("utf-8"), self.token.encode("utf-8")):
            raise BridgeError("UNAUTHORIZED", "Invalid session token")
        rid = request.get("id")
        if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
            raise BridgeError("BAD_REQUEST", "A request id is required")
        if not isinstance(request.get("method"), str):
            raise BridgeError("BAD_REQUEST", "A method is required")
        if not isinstance(request.get("params", {}), dict):
            raise BridgeError("BAD_REQUEST", "params must be an object")
        ttl = request.get("timeout_ms", 10000)
        if type(ttl) is not int or not 1 <= ttl <= 30000:
            raise BridgeError("BAD_REQUEST", "timeout_ms must be 1..30000")
        body = {k: v for k, v in request.items() if k not in ("token", "timeout_ms")}
        fingerprint = hashlib.sha256(json.dumps(
            body, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.lock:
            if rid in self.jobs:
                job = self.jobs[rid]
                if job.fingerprint != fingerprint:
                    raise BridgeError("ID_REUSED", "Request id already has different arguments")
                return job
            is_write = request["method"] in WRITE_METHODS
            if is_write and self.journal and self.record_path(rid).exists():
                record = json.loads(self.record_path(rid).read_text(encoding="utf-8"))
                if record["fingerprint"] != fingerprint:
                    raise BridgeError("ID_REUSED", "Request id already has different arguments")
                if record["state"] not in ("done", "expired"):
                    raise BridgeError("OPERATION_UNCERTAIN", "Recorded operation has no final result; will not replay")
                job = Job(request, fingerprint, time.monotonic())
                job.state, job.result = record["state"], record["result"]
                job.event.set()
                return job
            if is_write and rid in self.seen_writes:
                raise BridgeError("RESULT_EXPIRED", "Old write result was evicted; will not replay")
            if len(self.jobs) >= self.capacity:
                for old_id, old_job in list(self.jobs.items()):
                    if old_job.event.is_set():
                        del self.jobs[old_id]
                        break
                else:
                    raise BridgeError("BUSY", "Too many pending operations")
            job = Job(request, fingerprint, time.monotonic() + ttl / 1000)
            self.record(job)
            if is_write:
                if not self.journal:
                    self.seen_writes.add(rid)
            self.jobs[rid] = job
        self.dispatch(job)
        return job

    def execute(self, job, handler):
        with job.lock:
            if job.state != "queued":
                return
            if time.monotonic() >= job.deadline:
                job.state = "expired"
                job.result = failure("EXPIRED", "Request expired before execution")
                self.record(job)
                job.event.set()
                return
            job.state = "running"
            try:
                self.record(job)
            except OSError:
                job.state = "done"
                job.result = failure("JOURNAL_ERROR", "Cannot record operation; no mutation executed")
                job.event.set()
                return
        try:
            result = {"ok": True, "result": handler(job.request)}
        except BridgeError as exc:
            result = failure(exc.code, str(exc))
        except Exception as exc:
            result = failure("EXECUTION_ERROR", "%s: %s" % (type(exc).__name__, exc))
        with job.lock:
            job.result = result
            job.state = "done"
            try:
                self.record(job)
            except OSError:
                job.result = failure("OPERATION_UNCERTAIN", "Execution finished but recording failed; inspect document before retrying")
            job.event.set()

    def await_result(self, job):
        job.event.wait(max(0, job.deadline - time.monotonic()))
        with job.lock:
            if job.event.is_set():
                return job.result
            if job.state == "queued":
                job.state = "expired"
                job.result = failure("EXPIRED", "Request expired before execution")
                self.record(job)
                job.event.set()
                return job.result
            return failure("IN_PROGRESS", "Execution started; retry the same request id to query")


class LocalServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, broker):
        self.broker = broker
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 0), RequestHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.sendall(json.dumps(failure("BUSY", "Connection limit reached")).encode() + b"\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # A disconnected client must not terminate or corrupt the GUI session.
        pass


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(35)
        try:
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise BridgeError("BAD_REQUEST", "Message too large or missing newline")
            request = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
            job = self.server.broker.submit(request)
            response = self.server.broker.await_result(job)
        except BridgeError as exc:
            response = failure(exc.code, str(exc))
        except (ValueError, UnicodeError, TimeoutError) as exc:
            response = failure("BAD_REQUEST", str(exc))
        self.wfile.write(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")


def _reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)
