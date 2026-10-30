import json
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch

from veusz_bridge.desktop_host import Server, Handler, ensure_host


class DesktopHostTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.token = "test-only-token"
        self.server.host_id = "fixture-host"
        self.server.directory = (Path.cwd() / "runtime" / "host-fixture-windows").resolve()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def send(self, request):
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("rb") as stream:
                return json.loads(stream.readline())

    def test_unauthenticated_requests_cannot_launch_processes(self):
        with patch("bridge_cli.launch") as launch:
            result = self.send({"method": "launch", "token": "wrong"})
            self.assertFalse(result["ok"])
            launch.assert_not_called()

    def test_authenticated_launcher_is_scoped_to_managed_session_directory(self):
        with patch("bridge_cli.launch") as launch:
            result = self.send({"method": "launch", "token": self.server.token,
                                "session_file": str(Path.cwd() / "outside-session.json")})
            self.assertFalse(result["ok"])
            launch.assert_not_called()

    def test_native_window_launch_is_delegated_without_exposing_host_credentials(self):
        target = self.server.directory / "figure" / "session.json"
        with patch("bridge_cli.launch", return_value={"state": {"pid": 42}}) as launch:
            result = self.send({"method": "launch", "token": self.server.token,
                                "session_file": str(target), "label": "Figure A"})
            self.assertTrue(result["ok"])
            launch.assert_called_once_with(None, target, label="Figure A")
            self.assertNotIn(self.server.token, str(result))

    def test_ready_response_has_a_launcher_identity_distinct_from_a_python_shim_pid(self):
        result = self.send({"method": "ping", "token": self.server.token})
        self.assertEqual(result["result"]["host_id"], "fixture-host")
        self.assertNotIn(self.server.token, str(result))

    def test_mcp_cannot_silently_bootstrap_a_missing_desktop_host(self):
        with patch("veusz_bridge.desktop_host.request_host", side_effect=ConnectionRefusedError), \
                patch("veusz_bridge.desktop_host.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "DESKTOP_HOST_REQUIRED"):
                ensure_host(allow_start=False)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
