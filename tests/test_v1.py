import json
import os
from pathlib import Path
import tempfile
import unittest
import shutil
import uuid
from unittest.mock import patch

from veusz_bridge.dataio import read_table
from veusz_bridge.protocol import Broker, BridgeError
from veusz_bridge.client import RemoteError
from bridge_cli import launch, resolve_executable, window_readiness_error


class LocalTemporaryDirectory:
    """Keep Windows sandbox test files under the authorized workspace."""
    def __init__(self):
        self.root = Path(__file__).resolve().parents[1] / "runtime" / "unit"
        self.path = self.root / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def cleanup(self):
        assert self.root.resolve() in self.path.resolve().parents
        shutil.rmtree(self.path)

    def __enter__(self):
        return self.name

    def __exit__(self, *unused):
        self.cleanup()


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = LocalTemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def table(self, text, name="sample.csv"):
        p = self.root / name
        p.write_text(text, encoding="utf-8-sig")
        return p

    def test_unicode_tsv_and_hash(self):
        p = self.table("时间\t信号\n1\t3\n2\t4\n", "数据.tsv")
        result = read_table(p, {"x": "时间", "y": "信号"})
        self.assertEqual(result["datasets"], {"x": [1., 2.], "y": [3., 4.]})
        self.assertEqual(len(result["sha256"]), 64)

    def test_missing_policy_keeps_columns_aligned(self):
        p = self.table("x,y\n1,2\n2,\n3,4\n")
        with self.assertRaises(BridgeError):
            read_table(p, {"a": "x", "b": "y"})
        r = read_table(p, {"a": "x", "b": "y"}, missing="drop_rows")
        self.assertEqual(r["datasets"], {"a": [1., 3.], "b": [2., 4.]})
        self.assertEqual(r["dropped_rows"], 1)

    def test_nonfinite_values_are_not_silently_imported(self):
        for value in ("nan", "inf", "-inf"):
            p = self.table("x,y\n1," + value + "\n")
            with self.assertRaises(BridgeError):
                read_table(p, {"y": "y"})

    def test_duplicate_and_missing_headers_are_rejected(self):
        for text in ("x,x\n1,2\n", "x,z\n1,2\n"):
            with self.assertRaises(BridgeError):
                read_table(self.table(text), {"a": "y"})


class JournalTests(unittest.TestCase):
    def test_reads_and_writes_exceed_old_limit_with_bounded_memory(self):
        with LocalTemporaryDirectory() as directory:
            queue = []
            b = Broker("token", queue.append, capacity=8, journal=directory)
            first = None
            count = 0
            for i in range(1100):
                req = {"id": str(i), "token": "token", "method": "set" if i % 10 == 0 else "state"}
                job = b.submit(req)
                b.execute(job, lambda _: {"sequence": i})
                result = b.await_result(job)
                self.assertTrue(result["ok"], result)
                if i == 0:
                    first = req
                count += 1
            result = b.submit(first)
            self.assertEqual(b.await_result(result)["result"]["sequence"], 0)
            self.assertLessEqual(len(b.jobs), 8)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 110)
            self.assertEqual(len(queue), count)
            self.assertEqual(len(b.seen_writes), 0)

    def test_incomplete_persisted_write_is_never_reexecuted(self):
        with LocalTemporaryDirectory() as directory:
            req = {"id": "pending", "token": "t", "method": "set"}
            b = Broker("t", lambda job: None, journal=directory)
            b.submit(req)
            restarted = Broker("t", lambda job: self.fail("Replayed uncertain write"), journal=directory)
            with self.assertRaises(BridgeError) as caught:
                restarted.submit(req)
            self.assertEqual(caught.exception.code, "OPERATION_UNCERTAIN")

    def test_non_ascii_token_is_cleanly_rejected(self):
        with self.assertRaises(BridgeError) as caught:
            Broker("t", lambda job: None).submit({"token": "错误"})
        self.assertEqual(caught.exception.code, "UNAUTHORIZED")

    def test_journal_failure_before_execution_does_not_mutate(self):
        b = Broker("t", lambda job: None)
        job = b.submit({"id":"one", "token":"t", "method":"set"})
        with patch.object(b, "record", side_effect=OSError("disk unavailable")):
            b.execute(job, lambda req: self.fail("Mutation ran without a write-ahead record"))
        self.assertEqual(b.await_result(job)["error"]["code"], "JOURNAL_ERROR")


class ExecutableTests(unittest.TestCase):
    def setUp(self):
        directory = LocalTemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, kwargs in (
                ("bridge_cli.ROOT", {"new": self.root}),
                ("bridge_cli.shutil.which", {"return_value": None})):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def executable(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
        return path.resolve()

    def test_explicit_executable_overrides_environment(self):
        path = self.executable("chosen.exe")
        os.environ["VEUSZ_EXE"] = "missing.exe"
        self.assertEqual(resolve_executable(str(path)), path)

    def test_environment_overrides_local_configuration(self):
        path = self.executable("environment.exe")
        os.environ["VEUSZ_EXE"] = str(path)
        (self.root / "veusz.local.json").write_text('{"executable":"missing.exe"}')
        self.assertEqual(resolve_executable(), path)

    def test_local_configuration_supports_project_relative_paths(self):
        path = self.executable("portable/veusz.exe")
        (self.root / "veusz.local.json").write_text('{"executable":"portable/veusz.exe"}')
        self.assertEqual(resolve_executable(), path)

    def test_invalid_explicit_path_does_not_silently_select_other_installation(self):
        other = self.executable("other.exe")
        with patch("bridge_cli.shutil.which", return_value=str(other)) as search:
            with self.assertRaisesRegex(FileNotFoundError, "Configured Veusz executable"):
                resolve_executable("missing.exe")
            search.assert_not_called()

    def test_path_discovery_and_standard_installation(self):
        path = self.executable("programs/Veusz/veusz.exe")
        with patch("bridge_cli.shutil.which", return_value=str(path)):
            self.assertEqual(resolve_executable(), path)
        os.environ["ProgramFiles"] = str(self.root / "programs")
        self.assertEqual(resolve_executable(), path)

    def test_missing_installation_gives_configuration_instructions(self):
        with self.assertRaisesRegex(FileNotFoundError, "Set VEUSZ_EXE"):
            resolve_executable()

    def test_invalid_local_configuration_is_reported(self):
        (self.root / "veusz.local.json").write_text('{"executable":42}')
        with self.assertRaisesRegex(ValueError, "nonempty executable"):
            resolve_executable()
        (self.root / "veusz.local.json").write_text('[]')
        with self.assertRaisesRegex(ValueError, "JSON object"):
            resolve_executable()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        desktop = patch("bridge_cli.desktop_context", return_value={
            "desktop_is_interactive": True, "thread_desktop": "Default",
            "input_desktop": "Default", "desktop_error": None})
        self.desktop = desktop.start()
        self.addCleanup(desktop.stop)

    def test_desktop_readiness_requires_owned_visible_window_but_not_foreground(self):
        state = {"pid": 42, "window_handle": 100, "window_visible": True,
                 "window_native_exists": True, "window_native_visible": True,
                 "window_native_minimized": False, "window_native_on_screen": True,
                 "window_foreground": False, "window_native_pid": 42,
                 "desktop_is_interactive": True}
        self.assertIsNone(window_readiness_error(state, 42))

        for key, value, message in (
                ("window_handle", 0, "no native window handle"),
                ("window_native_visible", False, "window is hidden"),
                ("window_native_minimized", True, "is minimized"),
                ("window_native_on_screen", False, "outside the visible desktop"),
                ("desktop_is_interactive", False, "not verified on the user's input desktop"),
                ("window_native_pid", 7, "different process")):
            candidate = dict(state, **{key: value})
            self.assertIn(message, window_readiness_error(candidate, 42))

    def test_sandbox_launcher_refuses_before_spawning_or_changing_session(self):
        self.desktop.return_value = {
            "desktop_is_interactive": False, "thread_desktop": "CodexSandboxDesktop-test",
            "input_desktop": "Default", "desktop_error": None}
        with LocalTemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            original = '{"pid":123,"token":"keep"}'
            path.write_text(original)
            with patch("bridge_cli.subprocess.Popen") as spawn, patch("bridge_cli.Client") as client:
                with self.assertRaisesRegex(RuntimeError, "not on the user's interactive desktop"):
                    launch("A:/Veusz/veusz.exe", path)
                spawn.assert_not_called()
                client.assert_not_called()
            self.assertEqual(path.read_text(), original)
            self.assertFalse(path.with_suffix(".launch.lock").exists())

    def test_existing_isolated_window_is_not_reused_or_activated(self):
        with LocalTemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            original = '{"pid":123,"token":"keep"}'
            path.write_text(original)
            with patch("bridge_cli.Client") as client, \
                    patch("bridge_cli.subprocess.Popen") as spawn, \
                    patch("bridge_cli.request_window_foreground") as activate:
                client.return_value.state.return_value = {
                    "pid": 123, "window_handle": 100, "window_native_exists": True,
                    "window_native_pid": 123, "desktop_is_interactive": False,
                    "thread_desktop": "CodexSandboxDesktop-test", "input_desktop": "Default"}
                with self.assertRaisesRegex(RuntimeError, "not verified on the user's input desktop"):
                    launch("A:/Veusz/veusz.exe", path)
                spawn.assert_not_called()
                activate.assert_not_called()
            self.assertEqual(path.read_text(), original)

    @unittest.skipUnless(os.name == "nt", "Windows GUI startup contract")
    def test_new_launch_succeeds_when_native_window_is_visible_but_not_foreground(self):
        with LocalTemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "veusz.exe"
            executable.write_bytes(b"test")
            session_file = root / "session.json"
            process = unittest.mock.Mock(pid=42)
            process.poll.return_value = None
            state = {"pid": 42, "window_handle": 123, "window_visible": True,
                     "window_native_exists": True, "window_native_visible": True,
                     "window_native_minimized": False, "window_native_on_screen": True,
                     "window_foreground": False, "window_native_pid": 42,
                     "desktop_is_interactive": True}

            def spawn(*unused_args, **kwargs):
                Path(kwargs["env"]["VEUSZ_BRIDGE_SESSION_FILE"]).write_text("{}")
                return process

            with patch("bridge_cli.Client") as client, \
                    patch("bridge_cli.subprocess.Popen", side_effect=spawn) as popen, \
                    patch("bridge_cli.request_window_foreground") as foreground:
                client.return_value.state.return_value = state
                result = launch(str(executable), session_file)

            self.assertFalse(result["reused"])
            self.assertEqual(result["state"]["window_handle"], 123)
            self.assertEqual(popen.call_args.kwargs["startupinfo"].wShowWindow, 1)
            foreground.assert_called_once_with(state)

    def test_remote_error_does_not_spawn_or_destroy_existing_session(self):
        with LocalTemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            original = '{"pid":123,"token":"keep"}'
            path.write_text(original)
            with patch("bridge_cli.Client") as client, patch("bridge_cli.subprocess.Popen") as spawn:
                client.return_value.state.side_effect = RemoteError({"code":"BUSY", "message":"Dialog open"})
                with self.assertRaises(RemoteError):
                    launch("A:/Veusz/veusz.exe", path)
                spawn.assert_not_called()
            self.assertEqual(path.read_text(), original)

    def test_unreachable_live_process_does_not_spawn(self):
        with LocalTemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text('{"pid":123}')
            with patch("bridge_cli.Client") as client, patch("bridge_cli.process_running", return_value=True), patch("bridge_cli.subprocess.Popen") as spawn:
                client.return_value.state.side_effect = ConnectionRefusedError()
                with self.assertRaisesRegex(RuntimeError, "will not open"):
                    launch("A:/Veusz/veusz.exe", path)
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
