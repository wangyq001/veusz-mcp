"""Read back a real human edit, then prove subsequent bridge edits preserve it."""

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from veusz_bridge.client import Client, RemoteError

GRAPH = "/codex_demo/graph"
KEY = GRAPH + "/curve1/key"
COLOR2 = GRAPH + "/curve2/PlotLine/color"
WIDTH2 = GRAPH + "/curve2/PlotLine/width"
LEGEND = [GRAPH + "/legend/" + name for name in
          ("horzPosn", "vertPosn", "horzManual", "vertManual")]


def main():
    client = Client()
    baseline = json.loads((ROOT / "output/manual_baseline.json").read_text(encoding="utf-8"))
    state = client.state()
    assert state["document_id"] == baseline["state"]["document_id"], "Different document"
    before = client.call("get", {"paths": [KEY, COLOR2, WIDTH2] + LEGEND})
    assert before["values"][KEY] == "manual-edited", "Waiting for human to edit Key text"
    assert before["revision"] > baseline["state"]["revision"], "No human change observed"
    assert any(e["source"] == "gui" and e["revision"] > baseline["state"]["revision"]
               for e in state["events"]), "No native GUI change event observed"
    # This intentionally uses the state from BEFORE the human edit.
    try:
        client.call("set", {"changes": [{"path": KEY, "value": "must-not-overwrite"}]},
                    state=baseline["state"])
    except RemoteError as exc:
        assert exc.code == "REVISION_CONFLICT"
    else:
        raise AssertionError("A stale write overwrote the human edit")

    updated = client.call("set", {"changes": [{"path": COLOR2, "value": "#c026d3"},
                                               {"path": WIDTH2, "value": "2.5pt"}]},
                          state=client.state())
    after = client.call("get", {"paths": [KEY, COLOR2, WIDTH2] + LEGEND})
    for path in [KEY] + LEGEND:
        assert after["values"][path] == before["values"][path], "Human edit overwritten: " + path
    assert after["values"][COLOR2] == "#c026d3"
    assert after["values"][WIDTH2] == "2.5pt"
    # Undo the bridge operation and prove the human's preceding edit survives.
    client.call("undo", state=client.state())
    undone = client.call("get", {"paths": [KEY, COLOR2, WIDTH2] + LEGEND})
    assert undone["values"] == before["values"]
    # Leave a visible result of continued Codex editing in the open window.
    client.call("set", {"changes": [{"path": COLOR2, "value": "#c026d3"},
                                    {"path": WIDTH2, "value": "2.5pt"}]}, state=client.state())
    time.sleep(0.4)
    client.call("export", {"path": "output/final_demo.png"})
    client.call("export", {"path": "output/final_demo.svg"})
    client.call("snapshot", {"path": "output/final_window.png"})
    report_path = ROOT / "output/verification.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["human_edit"] = {"passed": True, "before": before, "after": after,
                            "gui_event_observed": True, "stale_write_rejected": True,
                            "subsequent_edit_preserved_human_settings": True,
                            "undo_preserved_human_settings": True}
    report["final_state"] = client.state()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"human_edit": "passed", "key_text": before["values"][KEY],
                      "continued_edit": "curve2 purple, 2.5pt", "undo": "passed",
                      "pid": updated["pid"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
