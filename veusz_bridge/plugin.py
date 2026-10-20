"""Qt-main-thread adapter for native Veusz with Qt5/Qt6. No UI automation."""

import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from io import StringIO

from veusz import document, qtall as qt, utils
from veusz.document import operations

from .protocol import BridgeError, Broker, LocalServer, WRITE_METHODS
from .desktop import desktop_context


class AtomicBatch(operations.OperationMultiple):
    """POC reversible operations only; compensate completed operations on error."""

    def do(self, doc):
        completed = []
        try:
            for op in self.operations:
                op.do(doc)
                completed.append(op)
        except Exception:
            for op in reversed(completed):
                op.undo(doc)
            raise


class Bridge(qt.QObject):
    requested = qt.pyqtSignal(object)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.window = None
        self.doc = None
        self.ci = None
        self.document_id = None
        self.revision = 0
        self.events = []
        self.source = "gui"
        self.bridge_id = str(uuid.uuid4())
        self.sources = []
        self.rendered = None
        self.render_jobs = {}
        self.root = Path(os.environ["VEUSZ_BRIDGE_ROOT"]).resolve()
        self.session_file = Path(os.environ["VEUSZ_BRIDGE_SESSION_FILE"])
        self.token = os.environ["VEUSZ_BRIDGE_TOKEN"]
        self.broker = Broker(self.token, self.enqueue,
                             journal=self.session_file.parent / "operations" / self.bridge_id)
        queued = (qt.Qt.QueuedConnection if hasattr(qt.Qt, "QueuedConnection")
                  else qt.Qt.ConnectionType.QueuedConnection)
        self.requested.connect(self.execute, queued)
        self.timer = qt.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.attach)
        self.timer.start()
        self.app.aboutToQuit.connect(self.shutdown)

    def attach(self):
        from veusz.windows.mainwindow import MainWindow

        windows = [w for w in MainWindow.windows if w.isVisible()]
        if len(windows) != 1:
            return
        self.window = windows[0]
        # The launcher is user-facing: make the editor window visible and ask
        # Windows to activate it before publishing the bridge session.
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        if hasattr(self, "test_preferences"):
            from veusz import setting
            for key, value in self.test_preferences.items():
                setting.settingdb[key] = value
            del self.test_preferences
        self.doc = self.window.document
        self.ci = document.CommandInterface(self.doc)
        self.document_id = str(uuid.uuid4())
        self.doc.signalModified.connect(self.modified)
        self.doc.sigWiped.connect(self.wiped)
        self.window.plot.rendercontrol.signalRenderFinished.connect(self.render_finished)
        self.timer.stop()
        self.server = LocalServer(self.broker)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        info = {"protocol": 2, "bridge_version": "0.1.0", "bridge_id": self.bridge_id,
                "host": "127.0.0.1", "port": self.server.server_address[1],
                "token": self.token, "pid": os.getpid(), "veusz_version": utils.version(),
                "document_id": self.document_id, "started_at": time.time()}
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(info), encoding="utf-8")
        os.replace(str(temporary), str(self.session_file))

    def shutdown(self):
        if hasattr(self, "server"):
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def modified(self, *unused):
        self.revision += 1
        self.events.append({"revision": self.revision, "source": self.source, "time": time.time()})
        self.events = self.events[-100:]

    def wiped(self):
        self.document_id = str(uuid.uuid4())
        self.rendered = None
        self.modified()

    def enqueue(self, job):
        # Called by the socket worker. Never touch any Qt/document objects here.
        if job.request["method"] == "import_data":
            from .dataio import read_table
            p = job.request.get("params", {})
            try:
                job.prepared = read_table(p["path"], p["columns"], p.get("delimiter"),
                                          p.get("missing", "error"), p.get("encoding", "utf-8-sig"))
            except Exception as exc:
                job.preparation_error = exc
        self.requested.emit(job)

    def render_finished(self, jobid, image, helper):
        identity = self.render_jobs.pop(jobid, None)
        if identity:
            self.rendered = identity
        for oldid in list(self.render_jobs):
            if oldid < jobid:
                del self.render_jobs[oldid]

    def refresh(self):
        jobid = self.window.plot.rendercontrol.latestaddedjob + 1
        self.render_jobs[jobid] = {"document_id": self.document_id, "revision": self.revision,
                                   "page": self.window.plot.pagenumber, "job_id": jobid}
        self.window.plot.actionForceUpdate()

    @qt.pyqtSlot(object)
    def execute(self, job):
        def run(request):
            if hasattr(job, "preparation_error"):
                raise job.preparation_error
            return self.handle(request, getattr(job, "prepared", None))
        self.broker.execute(job, run)

    def state(self):
        nodes = []

        def visit(widget):
            nodes.append({"path": widget.path, "type": widget.typename})
            for child in widget.children:
                visit(child)

        visit(self.doc.basewidget)
        modal = self.app.activeModalWidget()
        native = self.native_window_state()
        return {"document_id": self.document_id, "revision": self.revision,
                "bridge_version": "0.1.0", "bridge_id": self.bridge_id,
                "pid": os.getpid(), "veusz_version": utils.version(),
                "python": sys.version.split()[0], "qt": qt.QT_VERSION_STR,
                "window_class": type(self.window).__name__,
                "window_visible": self.window.isVisible(),
                **desktop_context(),
                **native,
                "editor_panels": {name: bool(getattr(self.window, name, None))
                                  for name in ("treeedit", "propdock", "formatdock", "datadock")},
                "modal": modal is not None, "modal_title": modal.windowTitle() if modal else None,
                "modified": self.doc.isModified(), "filename": self.doc.filename,
                "page": self.window.plot.pagenumber, "rendered": self.rendered,
                "datasets": self.ci.GetDatasets(), "widgets": nodes,
                "data_summary": {name: {"length": len(ds.data), "dimensions": ds.dimensions}
                                 for name, ds in self.doc.data.items()},
                "data_sources": self.sources,
                "events": self.events[-20:]}

    def native_window_state(self):
        """Report Win32 window facts; Qt isVisible alone does not prove a desktop window."""
        handle = int(self.window.winId())
        result = {"window_handle": handle, "window_native_exists": False,
                  "window_native_visible": False, "window_native_minimized": False,
                  "window_native_on_screen": False, "window_foreground": False,
                  "window_native_pid": None}
        if os.name != "nt":
            return result

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = wintypes.HWND(handle)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        rect = wintypes.RECT()
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        exists = bool(user32.IsWindow(hwnd))
        visible = bool(exists and user32.IsWindowVisible(hwnd))
        minimized = bool(exists and user32.IsIconic(hwnd))
        owner_pid = wintypes.DWORD()
        if exists:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        on_screen = False
        if exists and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            right = left + user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            bottom = top + user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            on_screen = (rect.right > rect.left and rect.bottom > rect.top and
                         rect.right > left and rect.left < right and
                         rect.bottom > top and rect.top < bottom)
        foreground = user32.GetForegroundWindow()
        foreground_handle = int(foreground or 0)
        return {"window_handle": handle, "window_native_exists": exists,
                "window_native_visible": visible, "window_native_minimized": minimized,
                "window_native_on_screen": on_screen,
                "window_foreground": foreground_handle == handle,
                "window_native_pid": int(owner_pid.value) if exists else None}

    def check_write(self, request):
        if request.get("document_id") != self.document_id:
            raise BridgeError("DOCUMENT_CHANGED", "Read the current document before writing")
        if request.get("expected_revision") != self.revision:
            raise BridgeError("REVISION_CONFLICT", "Document changed; read latest state before writing")
        if self.app.activeModalWidget() is not None:
            raise BridgeError("BUSY", "Finish the open Veusz dialog before writing")
        from veusz.windows.mainwindow import MainWindow
        if len([w for w in MainWindow.windows if w.isVisible()]) != 1:
            raise BridgeError("AMBIGUOUS_WINDOW", "POC requires exactly one window in this process")

    def output_path(self, name):
        if not isinstance(name, str) or not name:
            raise BridgeError("BAD_PATH", "An output path is required")
        path = (self.root / name).resolve()
        if self.root not in path.parents:
            raise BridgeError("BAD_PATH", "Outputs must stay inside the project")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def handle(self, request, prepared=None):
        if qt.QThread.currentThread() != self.thread():
            raise BridgeError("WRONG_THREAD", "Document access requires the Qt main thread")
        if self.window is None or not self.window.isVisible():
            raise BridgeError("WINDOW_CLOSED", "The connected window has been closed")
        method = request["method"]
        params = request.get("params", {})
        if method == "operation":
            return self.broker.status(params["request_id"])
        if method == "state":
            return self.state()
        if method == "get":
            paths = params.get("paths", [])
            if not isinstance(paths, list) or len(paths) > 100:
                raise BridgeError("BAD_REQUEST", "Provide at most 100 setting paths")
            return {"document_id": self.document_id, "revision": self.revision,
                    "values": {p: self.ci.Get(p) for p in paths}}
        if method == "describe":
            path = params["path"]
            node = self.doc.resolvePath(self.doc.basewidget, path)
            if node.iswidget:
                node = node.settings
            if not node.issettings:
                raise BridgeError("BAD_REQUEST", "Expected a widget or settings group")
            settings = []
            for child in node.getList():
                settings.append({"name": child.name, "path": path.rstrip("/") + "/" + child.name,
                                 "group": child.issettings,
                                 "value": None if child.issettings else child.val})
            return {"settings": settings, "revision": self.revision}
        if method == "snapshot":
            self.check_write(request)
            path = self.output_path(params.get("path", "output/window.png"))
            if not self.window.grab().save(str(path)):
                raise BridgeError("SAVE_FAILED", "Could not capture native Veusz window")
            return {"path": str(path), "revision": self.revision}
        if method == "export":
            self.check_write(request)
            path = self.output_path(params.get("path", "output/preview.png"))
            if path.suffix.lower() not in (".png", ".svg", ".pdf"):
                raise BridgeError("BAD_PATH", "POC supports PNG, SVG and PDF")
            page = params.get("page", self.window.plot.pagenumber)
            if type(page) is not int or not 0 <= page < self.doc.getNumberPages():
                raise BridgeError("BAD_PAGE", "Page index is zero-based and must exist")
            dpi = params.get("dpi", 150)
            if type(dpi) is not int or not 50 <= dpi <= 1200:
                raise BridgeError("BAD_REQUEST", "DPI must be 50..1200")
            self.ci.Export(str(path), page=page, dpi=dpi,
                           backcolor=params.get("background", "#ffffffff"))
            return {"path": str(path), "revision": self.revision}
        if method not in WRITE_METHODS:
            raise BridgeError("UNKNOWN_METHOD", "Unsupported method: " + method)
        self.check_write(request)
        self.source = "bridge"
        try:
            if method == "demo":
                self.create_demo()
            elif method == "set":
                self.set_batch(params)
            elif method == "undo":
                self.undo_last_bridge_operation()
            elif method == "import_data":
                self.import_prepared(prepared, params)
            elif method == "plot":
                self.create_plot(params)
            elif method == "add_widgets":
                self.add_widgets(params)
            elif method in ("save", "checkpoint"):
                path = self.save_document(params, checkpoint=method == "checkpoint")
                return {"path": str(path), "state": self.state()}
            elif method == "open":
                self.open_document(params)
            elif method == "close":
                if self.doc.isModified():
                    raise BridgeError("UNSAVED_CHANGES", "Save the document before closing")
                qt.QTimer.singleShot(100, self.window.close)
                return {"closing": True}
        finally:
            self.source = "gui"
        self.refresh()
        result = self.state()
        result["request_id"] = request["id"]
        return result

    def apply(self, ops, description):
        batch = AtomicBatch(ops, descr=description)
        self.doc.applyOperation(batch)
        self.last_operation = batch
        self.last_operation_revision = self.revision

    def set_batch(self, params):
        changes = params.get("changes")
        if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
            raise BridgeError("BAD_REQUEST", "Provide 1..100 setting changes")
        ops = []
        for change in changes:
            path = change["path"]
            if not isinstance(path, str) or not path.startswith("/"):
                raise BridgeError("BAD_REQUEST", "Setting paths must be absolute")
            setting = self.doc.resolveSettingPath(self.doc.basewidget, path)
            value = change["value"]
            # This is the same type/value normalizer used by Veusz's settings.
            normalized = setting.normalize(value)
            ops.append(operations.OperationSettingSet(setting, normalized))
        self.apply(ops, "Codex: update settings")

    def undo_last_bridge_operation(self):
        if (not hasattr(self, "last_operation") or
                self.last_operation_revision != self.revision or
                not self.doc.historyundo or
                self.doc.historyundo[-1] is not self.last_operation):
            raise BridgeError("UNDO_CONFLICT", "Latest operation is not an unchanged bridge operation")
        self.doc.undoOperation()
        self.last_operation = None

    def grouped(self, action, description):
        batch = operations.OperationMultiple([], descr=description)
        original_data = dict(self.doc.data)
        original_history = (list(self.doc.historyundo), list(self.doc.historyredo))
        original_dirty = (self.doc.modified, self.doc.changeset)
        children = []
        def remember(widget):
            children.append((widget, list(widget.children)))
            for child in widget.children:
                remember(child)
        remember(self.doc.basewidget)
        self.doc.batchHistory(batch)
        self.doc.suspendUpdates()
        try:
            result = action()
        except Exception:
            rollback_errors = []
            for op in reversed(batch.operations):
                try:
                    op.undo(self.doc)
                except Exception as exc:
                    rollback_errors.append(str(exc))
            # A failed constructor can attach a widget before an Operation has
            # reached the undo log. Also remove those unrecorded new children.
            for widget, original_children in children:
                for child in widget.children:
                    if child not in original_children:
                        child.parent = None
                widget.children[:] = original_children
                for child in original_children:
                    child.parent = widget
            self.doc.data = original_data
            self.doc.historyundo, self.doc.historyredo = original_history
            if rollback_errors:
                raise BridgeError("ROLLBACK_FAILED", "Inspect document before further edits: " + "; ".join(rollback_errors))
            self.doc.modified, self.doc.changeset = original_dirty
            raise
        finally:
            self.doc.batchHistory(None)
            self.doc.enableUpdates()
        self.doc.historyundo = self.doc.historyundo[-9:] + [batch]
        self.doc.historyredo = []
        self.last_operation = batch
        self.last_operation_revision = self.revision
        return result

    def import_prepared(self, prepared, params):
        if not prepared:
            raise BridgeError("INVALID_DATA", "No prepared data")
        names = list(prepared["datasets"])
        if not params.get("replace", False) and any(name in self.doc.data for name in names):
            raise BridgeError("DATA_EXISTS", "Dataset already exists; replace must be explicit")
        def action():
            for name, values in prepared["datasets"].items():
                self.ci.SetData(name, values)
        self.grouped(action, "Codex: import numeric data")
        self.sources.append({k: v for k, v in prepared.items() if k != "datasets"})
        self.sources = self.sources[-100:]

    def add_widgets(self, params):
        entries = params.get("widgets", [])
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise BridgeError("BAD_REQUEST", "Provide 1..100 widgets")
        allowed = {"page", "graph", "axis", "xy", "key", "label"}
        for entry in entries:
            if entry.get("type") not in allowed:
                raise BridgeError("BAD_WIDGET", "Supported widget types: " + ", ".join(sorted(allowed)))
            if not entry.get("name") or "/" in entry["name"]:
                raise BridgeError("BAD_WIDGET", "A nonempty widget name without slashes is required")
        def action():
            for entry in entries:
                parent = entry.get("parent", "/")
                widget = self.doc.resolveWidgetPath(self.doc.basewidget, parent)
                if entry["name"] in widget.childnames:
                    raise BridgeError("WIDGET_EXISTS", "Widget already exists: " + entry["name"])
                self.ci.Add(entry["type"], widget=parent, name=entry["name"], **entry.get("settings", {}))
        self.grouped(action, "Codex: add widgets")

    def create_plot(self, params):
        series = params.get("series", [])
        if not isinstance(series, list) or not 1 <= len(series) <= 32:
            raise BridgeError("BAD_REQUEST", "Provide 1..32 series")
        import numpy as np
        current_graph = params.get("graph_path")
        x_log = params.get("x_log", self.ci.Get(current_graph + "/x/log") if current_graph else False)
        y_log = params.get("y_log", self.ci.Get(current_graph + "/y/log") if current_graph else False)
        for entry in series:
            if entry.get("kind", "line") not in ("line", "scatter", "errorbar"):
                raise BridgeError("BAD_REQUEST", "Series kind must be line, scatter or errorbar")
            x = self.doc.data.get(entry.get("x"))
            y = self.doc.data.get(entry.get("y"))
            if x is None or y is None or x.dimensions != 1 or y.dimensions != 1 or len(x.data) != len(y.data):
                raise BridgeError("INVALID_DATA", "Series requires existing equal-length 1D x/y datasets")
            if not len(x.data) or not np.all(np.isfinite(x.data)) or not np.all(np.isfinite(y.data)):
                raise BridgeError("INVALID_DATA", "Plot datasets must be nonempty and finite")
            if x_log and np.any(x.data <= 0) or y_log and np.any(y.data <= 0):
                raise BridgeError("INVALID_LOG_DATA", "Log axes require strictly positive values")
            if entry.get("error"):
                error = self.doc.data.get(entry["error"])
                if error is None or error.dimensions != 1 or len(error.data) != len(y.data) or not np.all(np.isfinite(error.data)) or np.any(error.data < 0):
                    raise BridgeError("INVALID_ERROR_DATA", "Error values must be nonnegative and match y")
        def action():
            graph = params.get("graph_path")
            if not graph:
                name = params.get("page_name", "figure1")
                if not isinstance(name, str) or not name or "/" in name or name in self.doc.basewidget.childnames:
                    raise BridgeError("WIDGET_EXISTS", "Choose a new page_name without slashes")
                self.ci.Add("page", widget="/", name=name, width="16cm", height="11cm")
                graph = "/" + name + "/graph"
                self.ci.Add("graph", widget="/" + name, name="graph", leftMargin="1.8cm",
                            bottomMargin="1.5cm", rightMargin="0.8cm", topMargin="0.8cm")
            target = self.doc.resolveWidgetPath(self.doc.basewidget, graph)
            if target.typename != "graph":
                raise BridgeError("BAD_WIDGET", "graph_path must identify a graph")
            for key, setting in (("x_label", "x/label"), ("y_label", "y/label"),
                                 ("x_log", "x/log"), ("y_log", "y/log")):
                if key in params:
                    self.ci.Set(graph + "/" + setting, params[key])
            for index, entry in enumerate(series, start=1):
                name = entry.get("name", "curve%d" % index)
                if name in target.childnames or not name or "/" in name:
                    raise BridgeError("WIDGET_EXISTS", "Choose a new series name")
                yname = entry["y"]
                if entry.get("error"):
                    yname = "__" + name + "_with_error"
                    if yname in self.doc.data:
                        raise BridgeError("DATA_EXISTS", "Internal error dataset already exists")
                    self.ci.SetData(yname, self.doc.data[entry["y"]].data,
                                    symerr=self.doc.data[entry["error"]].data)
                kind = entry.get("kind", "line")
                options = {"PlotLine__width": entry.get("width", "1.5pt"),
                           "marker": "circle" if kind in ("scatter", "errorbar") else "none",
                           "PlotLine__hide": kind == "scatter"}
                if "color" in entry:
                    options.update(PlotLine__color=entry["color"], MarkerFill__color=entry["color"])
                self.ci.Add("xy", widget=graph, name=name, xData=entry["x"], yData=yname,
                            key=entry.get("label", name), **options)
            if not any(w.typename == "key" for w in target.children):
                self.ci.Add("key", widget=graph, name="legend")
            page = target
            while page.typename != "page":
                page = page.parent
            self.window.plot.setPageNumber(self.doc.basewidget.children.index(page))
        self.grouped(action, "Codex: create plot")
        self.window.plot.slotViewZoomPage()

    def serialize(self, path):
        # Native serialization clears dirty state. Suspend notifications and
        # restore bookkeeping BEFORE re-enabling updates, including on failure.
        stream = StringIO()
        stream.name = str(path)
        modified, changeset, filename = self.doc.modified, self.doc.changeset, self.doc.filename
        self.doc.suspendUpdates()
        try:
            self.doc.saveToFile(stream)
            return stream.getvalue()
        finally:
            self.doc.modified, self.doc.changeset, self.doc.filename = modified, changeset, filename
            self.doc.enableUpdates()

    def save_document(self, params, checkpoint=False):
        default = "runtime/checkpoints/%s-%s.vsz" % (self.document_id, self.revision)
        path = self.output_path(params.get("path", default if checkpoint else "output/figure.vsz"))
        if path.suffix.lower() != ".vsz":
            raise BridgeError("BAD_PATH", "Native document extension must be .vsz")
        if path.exists() and not params.get("overwrite", False):
            raise BridgeError("FILE_EXISTS", "Set overwrite explicitly to replace an existing file")
        text = self.serialize(path)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()
        manifest = {"bridge_version": "0.1.0", "data_sources": self.sources,
                    "document_id": self.document_id, "revision": self.revision}
        path.with_suffix(".vsz.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if not checkpoint:
            self.doc.filename = self.window.filename = str(path)
            self.doc.setModified(False)
            self.window.updateTitlebar()
        return path

    def open_document(self, params):
        if self.doc.isModified():
            raise BridgeError("UNSAVED_CHANGES", "Save current changes before opening a document")
        path = self.output_path(params["path"])
        if path.suffix.lower() != ".vsz" or not path.is_file():
            raise BridgeError("BAD_PATH", "Choose an existing .vsz document in the project")
        candidate = document.Document()
        try:
            candidate.load(str(path), callbackunsafe=lambda: False,
                           callbackimporterror=lambda filename, error: None)
        except Exception as exc:
            raise BridgeError("OPEN_FAILED", "Document preflight failed without changing current document: " + str(exc))
        # Keep the existing Document instance so native panels and signals remain
        # connected. Veusz's native loader retains its trust checks.
        self.window.openFileInWindow(str(path))
        if self.doc.filename != str(path):
            raise BridgeError("OPEN_FAILED", "Veusz did not open the requested document")
        manifest = path.with_suffix(".vsz.json")
        self.sources = json.loads(manifest.read_text(encoding="utf-8")).get("data_sources", []) if manifest.exists() else []

    def create_demo(self):
        # Add a dedicated demo page. Never wipe a user's existing document.
        name = "codex_demo"
        if name in self.doc.basewidget.childnames:
            raise BridgeError("DEMO_EXISTS", "Demo already exists; do not recreate it")
        batch = operations.OperationMultiple([], descr="Codex: create live demo")
        self.doc.batchHistory(batch)
        self.doc.suspendUpdates()
        try:
            self.ci.Add("page", widget="/", name=name, width="16cm", height="11cm")
            page = "/" + name
            self.ci.Add("graph", widget=page, name="graph")
            graph = page + "/graph"
            self.ci.Set(graph + "/x/label", "Time (s)")
            self.ci.Set(graph + "/y/label", "Signal (a.u.)")
            self.ci.Set(graph + "/leftMargin", "1.8cm")
            self.ci.Set(graph + "/bottomMargin", "1.5cm")
            self.ci.Set(graph + "/rightMargin", "0.8cm")
            self.ci.Set(graph + "/topMargin", "0.8cm")
            import numpy as np
            x = np.linspace(0, 10, 101)
            for dataset in ("bridge_x", "bridge_y1", "bridge_y2"):
                if dataset in self.doc.data:
                    raise BridgeError("DATA_EXISTS", "Demo dataset already exists: " + dataset)
            self.ci.SetData("bridge_x", x)
            self.ci.SetData("bridge_y1", np.sin(x))
            self.ci.SetData("bridge_y2", 0.7 * np.cos(x))
            self.ci.Add("xy", widget=graph, name="curve1", xData="bridge_x", yData="bridge_y1",
                        key="sin(x)", marker="none", PlotLine__color="#2563eb", PlotLine__width="1.5pt")
            self.ci.Add("xy", widget=graph, name="curve2", xData="bridge_x", yData="bridge_y2",
                        key="0.7 cos(x)", marker="none", PlotLine__color="#e87924", PlotLine__width="1.5pt")
            self.ci.Add("key", widget=graph, name="legend")
        except Exception:
            completed = list(batch.operations)
            for op in reversed(completed):
                op.undo(self.doc)
            raise
        finally:
            self.doc.batchHistory(None)
            self.doc.enableUpdates()
        # The executed children were grouped by batchHistory; register that group
        # once without executing it again or discarding the previous history.
        self.doc.historyundo = self.doc.historyundo[-9:] + [batch]
        self.doc.historyredo = []
        self.last_operation = batch
        self.last_operation_revision = self.revision
        self.window.plot.setPageNumber(self.doc.getNumberPages() - 1)
        self.window.plot.slotViewZoomPage()
        widget = self.doc.resolveWidgetPath(self.doc.basewidget, graph + "/curve1")
        self.window.treeedit.selectWidget(widget)


def install():
    app = qt.QApplication.instance()
    if not hasattr(app, "_codex_veusz_bridge"):
        app._codex_veusz_bridge = Bridge(app)
