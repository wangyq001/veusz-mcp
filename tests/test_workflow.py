"""Optional Skill guidance and backwards-compatible MCP delivery contracts."""
import copy
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import veusz_mcp
from veusz_bridge.protocol import Broker, BridgeError, LocalServer

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_status_preserves_document_and_requires_no_skill(self):
        state = {"document_id": "human-document", "revision": 17,
                 "modified": True, "filename": "human.vsz", "widgets": []}
        original = copy.deepcopy(state)
        with patch.object(veusz_mcp, "invoke", return_value=state) as invoke:
            result = veusz_mcp.veusz_status()
        invoke.assert_called_once_with("state", window_id=None)
        self.assertEqual(state, original)
        self.assertEqual({key: result[key] for key in original}, original)
        workflow = result["plotting_workflow"]
        self.assertTrue(workflow["skill_optional"])
        self.assertIsNone(workflow["bound_skill"])
        self.assertEqual(workflow["skill_selection"], "user_or_client")
        self.assertEqual(workflow["output_backend"], "veusz_native")
        self.assertTrue(workflow["editable_output"])

    def test_disconnected_document_is_not_hidden_by_workflow_metadata(self):
        with patch.object(veusz_mcp, "invoke", side_effect=ToolError("Disconnected")):
            with self.assertRaisesRegex(ToolError, "Disconnected"):
                veusz_mcp.veusz_status()


class WorkflowProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_delivers_guidance_without_a_skill_or_gui_mutation(self):
        # The real MCP server/client use a simulated read-only bridge, so this
        # contract test needs no Veusz window and cannot modify a user's graph.
        calls = []
        state = {"bridge_version": "0.1.0", "document_id": "fixture-document",
                 "revision": 17, "modified": True, "filename": "fixture.vsz"}

        def handle(request):
            calls.append(request["method"])
            if request["method"] != "state":
                raise BridgeError("TEST_READ_ONLY", "Only state is allowed")
            return dict(state)

        token = secrets.token_hex(32)
        broker = Broker(token, lambda job: broker.execute(job, handle))
        server = LocalServer(broker)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        temporary_root = ROOT / "runtime" / "unit"
        temporary_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
                self.assertIn(temporary_root.resolve(), Path(directory).resolve().parents)
                session_file = Path(directory) / "session.json"
                session_file.write_text(json.dumps({"host": "127.0.0.1",
                    "port": server.server_address[1], "token": token,
                    "bridge_version": "0.1.0"}), encoding="utf-8")
                env = dict(os.environ, VEUSZ_SESSION_FILE=str(session_file),
                           PYTHONIOENCODING="utf-8")
                parameters = StdioServerParameters(command=sys.executable,
                    args=[str(ROOT / "veusz_mcp.py")], cwd=str(ROOT), env=env)
                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        self.assertIn(veusz_mcp.PLOTTING_GUIDANCE, initialized.instructions)
                        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                        self.assertTrue({"veusz_widget_catalog", "veusz_set_data", "veusz_widget_action",
                                         "veusz_file_formats", "veusz_import_file"} <= tools.keys())
                        for name in ("veusz_status", "veusz_plot", "veusz_add_widgets",
                                     "veusz_apply_changes"):
                            self.assertIn("Skill", tools[name].description)
                            self.assertNotIn("skill", tools[name].inputSchema.get("required", []))
                        response = await session.call_tool("veusz_status", {})
                        self.assertFalse(response.isError, str(response.content))
                        result = response.structuredContent
                        self.assertEqual({key: result[key] for key in state}, state)
                        self.assertTrue(result["plotting_workflow"]["skill_optional"])
                        self.assertIsNone(result["plotting_workflow"]["bound_skill"])
                        self.assertEqual(result["plotting_workflow"]["guidance"],
                                         veusz_mcp.PLOTTING_GUIDANCE)
                self.assertEqual(calls, ["state"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
