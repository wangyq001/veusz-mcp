import unittest
from pathlib import Path
from unittest.mock import patch

from veusz_bridge import windows
from veusz_bridge.protocol import BridgeError


class WindowRoutingTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {"window_id": "a", "path": Path("a/session.json"), "available": True,
             "state": {"document_id": "doc-a", "pid": 1, "token": "never-return"}},
            {"window_id": "b", "path": Path("b/session.json"), "available": True,
             "state": {"document_id": "doc-b", "pid": 2}}]
        mock = patch.object(windows, "records", return_value=self.records)
        mock.start()
        self.addCleanup(mock.stop)
        metadata = patch.object(windows, "session_metadata", side_effect=lambda: [
            {"path": r["path"], "window_id": r["window_id"], "info": {"pid": r["state"]["pid"]}}
            for r in self.records])
        metadata.start()
        self.addCleanup(metadata.stop)
        alive = patch.object(windows, "process_alive", side_effect=lambda info: next(
            r["available"] for r in self.records if r["state"]["pid"] == info["pid"]))
        alive.start()
        self.addCleanup(alive.stop)

    def test_explicit_window_and_document_routing(self):
        self.assertEqual(windows.select_session("a"), Path("a/session.json"))
        self.assertEqual(windows.select_session(document_id="doc-b"), Path("b/session.json"))

    def test_explicit_target_does_not_probe_unrelated_busy_editors(self):
        with patch.object(windows, "records", side_effect=AssertionError("Probed unrelated editors")):
            self.assertEqual(windows.select_session("a"), Path("a/session.json"))

    def test_multiple_windows_never_use_a_global_active_window(self):
        with self.assertRaises(BridgeError) as caught:
            windows.select_session()
        self.assertEqual(caught.exception.code, "AMBIGUOUS_WINDOW")

    def test_closed_or_unknown_target_does_not_fall_back(self):
        self.records[1]["available"] = False
        for target in ("b", "unknown"):
            with self.assertRaises(BridgeError) as caught:
                windows.select_session(target)
            self.assertEqual(caught.exception.code, "WINDOW_UNAVAILABLE")
        self.assertEqual(windows.select_session(), Path("a/session.json"))

    def test_stale_document_with_multiple_editors_is_rejected(self):
        with self.assertRaises(BridgeError) as caught:
            windows.select_session(document_id="old-document")
        self.assertEqual(caught.exception.code, "DOCUMENT_CHANGED")

    def test_window_inventory_does_not_return_tokens_or_connection_paths(self):
        text = str(windows.list_windows())
        self.assertNotIn("never-return", text)
        self.assertNotIn("session.json", text)
        self.assertEqual(len(windows.list_windows()["windows"]), 2)

    def test_creation_request_id_has_a_stable_private_session_path(self):
        with patch("veusz_bridge.desktop_host.launch_window", return_value={"state": {"bridge_id": "created"}}) as launch, \
                patch.object(windows, "window_directory", return_value=Path("runtime/nonexistent-fixture")):
            first = windows.create_window("Chart A", request_id="stable-request")
            path = launch.call_args.args[0]
            second = windows.create_window("Chart A", request_id="stable-request")
            self.assertEqual(path, launch.call_args.args[0])
            self.assertEqual(first, second)
            self.assertEqual(launch.call_args.args[2], "Chart A")

    def test_invalid_creation_metadata_is_rejected(self):
        for name, request in (("", None), ("x" * 81, None), ("A", ""), ("A", "x" * 129)):
            with self.assertRaises(BridgeError):
                windows.create_window(name, request_id=request)


if __name__ == "__main__":
    unittest.main()
