"""Exercise the real Veusz editor. Run before the human-edit acceptance step."""

import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from veusz_bridge.client import Client, RemoteError

GRAPH = "/codex_demo/graph"
COLOR = GRAPH + "/curve1/PlotLine/color"
WIDTH = GRAPH + "/curve1/PlotLine/width"
COLOR2 = GRAPH + "/curve2/PlotLine/color"


def main():
    client = Client()
    report = {"checks": [], "human_edit": "pending", "timestamp": time.time()}
    output = ROOT / "output"
    output.mkdir(exist_ok=True)

    def passed(name, **details):
        report["checks"].append({"name": name, "passed": True, **details})
        print("PASS", name)

    def values():
        return client.call("get", {"paths": [COLOR, WIDTH, COLOR2]})["values"]

    def expect_error(code, method, params, state):
        try:
            client.call(method, params, state=state)
        except RemoteError as exc:
            assert exc.code == code, (exc.code, code)
        else:
            raise AssertionError("Expected " + code)

    try:
        state = client.state()
        assert not state["modal"], "Close Veusz startup dialogs first"
        assert state["window_class"] == "MainWindow"
        assert state["window_visible"] and all(state["editor_panels"].values())
        report["environment"] = {k: state[k] for k in ("pid", "veusz_version", "python", "qt", "document_id")}
        passed("full_editor_connected")

        if not any(w["path"] == "/codex_demo" for w in state["widgets"]):
            state = client.call("demo", state=state)
        assert all(x in state["datasets"] for x in ("bridge_x", "bridge_y1", "bridge_y2"))
        assert any(w["path"] == GRAPH + "/legend" for w in state["widgets"])
        passed("native_demo_objects_and_datasets")

        settings = client.call("describe", {"path": GRAPH + "/legend"})["settings"]
        assert any(s["path"] == GRAPH + "/legend/horzPosn" for s in settings)
        passed("setting_group_inspection")

        baseline = values()
        time.sleep(0.4)
        client.call("snapshot", {"path": "output/live_before.png"})
        stale_state = client.state()
        start = time.perf_counter()
        changed = client.call("set", {"changes": [{"path": COLOR, "value": "#dc2626"},
                                                   {"path": WIDTH, "value": "3pt"}]}, state=stale_state)
        latency = (time.perf_counter() - start) * 1000
        assert values()[COLOR] == "#dc2626"
        assert values()[WIDTH] == "3pt"
        time.sleep(0.4)
        client.call("snapshot", {"path": "output/live_after.png"})
        before = hashlib.sha256((output / "live_before.png").read_bytes()).hexdigest()
        after = hashlib.sha256((output / "live_after.png").read_bytes()).hexdigest()
        assert before != after, "Native window did not change after modifying the document"
        passed("batch_update_and_native_window_refresh", commit_ms=round(latency, 2),
               observation_wait_ms=400)

        expect_error("REVISION_CONFLICT", "set", {"changes": [{"path": COLOR2, "value": "black"}]}, stale_state)
        assert values()[COLOR2] == baseline[COLOR2]
        passed("stale_write_rejected_without_overwrite")

        wrong_document = dict(changed, document_id="unrelated-document")
        expect_error("DOCUMENT_CHANGED", "set", {"changes": [{"path": COLOR, "value": "black"}]}, wrong_document)
        passed("wrong_document_rejected")

        state = client.state()
        current = values()
        try:
            client.call("set", {"changes": [{"path": COLOR, "value": "#000000"},
                                             {"path": WIDTH, "value": "invalid-distance"}]}, state=state)
        except RemoteError:
            pass
        else:
            raise AssertionError("Invalid width was accepted")
        assert values() == current
        assert client.state()["revision"] == state["revision"]
        passed("invalid_batch_has_no_partial_changes")

        client.call("undo", state=client.state())
        assert values() == baseline
        passed("one_step_native_undo_restores_entire_batch")

        state = client.state()
        request = client.make_request("set", {"changes": [{"path": COLOR, "value": "#16a34a"}]}, state=state)
        # Deliberately discard the response after sending a write.
        with socket.create_connection((client.session["host"], client.session["port"]), timeout=3) as sock:
            sock.sendall(json.dumps(request).encode() + b"\n")
        recovered = Client().send(request)
        assert recovered["revision"] == state["revision"] + 1
        assert Client().send(request) == recovered
        assert client.state()["revision"] == recovered["revision"]
        passed("lost_response_and_reconnect_do_not_repeat_mutation")

        child = subprocess.run([sys.executable, str(ROOT / "bridge_cli.py"), "call", "state"],
                               capture_output=True, text=True, encoding="utf-8", check=True)
        reconnected = json.loads(child.stdout)
        assert reconnected["pid"] == state["pid"]
        assert reconnected["document_id"] == state["document_id"]
        passed("new_client_process_reconnects_to_same_gui_and_document")

        client.call("undo", state=client.state())
        assert values() == baseline
        client.call("export", {"path": "output/demo.png"})
        client.call("export", {"path": "output/demo.svg"})
        assert (output / "demo.png").stat().st_size > 1000
        assert "<svg" in (output / "demo.svg").read_text(encoding="utf-8")
        passed("native_png_and_svg_export")

        report["manual_baseline"] = {"state": client.state(), "values": values()}
        (output / "manual_baseline.json").write_text(json.dumps(report["manual_baseline"], ensure_ascii=False, indent=2), encoding="utf-8")
        report["automated_passed"] = True
    except Exception:
        report["automated_passed"] = False
        report["error"] = traceback.format_exc()
        raise
    finally:
        (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
