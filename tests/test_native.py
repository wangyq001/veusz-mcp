"""Contracts for runtime widget coverage and typed data dispatch (no Qt required)."""
import unittest
from unittest.mock import Mock, patch

import veusz_mcp
from veusz_bridge.native import json_value, validate_widgets
from veusz_bridge.protocol import BridgeError, WRITE_METHODS


class NativeContracts(unittest.TestCase):
    def factory(self):
        return Mock(listWidgets=lambda: ["page", "graph3d", "future_plugin_chart"])

    def test_runtime_plugin_type_is_supported_without_code_changes(self):
        validate_widgets([dict(type="future_plugin_chart", parent="/page/graph", name="plot")], self.factory())

    def test_invalid_types_paths_and_reserved_settings_are_rejected(self):
        for entry in [dict(type="unknown", name="a"), dict(type="page", name=".."),
                      dict(type="page", name=5), dict(type="page", name="a", parent="relative"),
                      dict(type="page", name="a", settings={"widget": "/wrong"}),
                      dict(type="page", name="a", autoadd="false")]:
            with self.subTest(entry=entry), self.assertRaises(BridgeError):
                validate_widgets([entry], self.factory())

    def test_nonfinite_defaults_are_strict_json_compatible(self):
        self.assertEqual(json_value({"x": [1., float("nan"), float("inf")]}), {"x": [1., None, None]})

    def test_new_mutations_use_write_guards_and_operation_journal(self):
        self.assertIn("set_data", WRITE_METHODS)
        self.assertIn("widget_action", WRITE_METHODS)
        self.assertNotIn("widget_catalog", WRITE_METHODS)
        with patch.object(veusz_mcp, "invoke") as invoke:
            entries = [dict(name="z", type="2d", data=[[1, 2], [3, 4]])]
            veusz_mcp.veusz_set_data(entries, "doc", 7, request_id="stable")
            invoke.assert_called_once_with("set_data", dict(datasets=entries, replace=False), "doc", 7, "stable")
        with patch.object(veusz_mcp, "invoke") as invoke:
            veusz_mcp.veusz_widget_action("/fit", "fit", "doc", 8, "action")
            invoke.assert_called_once_with("widget_action", dict(path="/fit", action="fit"), "doc", 8, "action")


if __name__ == "__main__":
    unittest.main()
