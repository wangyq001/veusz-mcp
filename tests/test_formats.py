import tempfile
import unittest
from pathlib import Path

from veusz_bridge.formats import import_arguments, import_methods, export_options, prepare_source
from veusz_bridge.protocol import BridgeError


class ImporterFixture:
    import_commands = ["ImportFile", "ImportString", "ImportFilePlugin"]
    import_filenamearg = {"ImportFile": 0, "ImportString": -1, "ImportFilePlugin": 1}

    def ImportFile(self, filename, descriptor, linked=False):
        pass

    def ImportString(self, descriptor, dstring):
        pass

    def ImportFilePlugin(self, plugin, filename, **args):
        pass


class FileFormatTests(unittest.TestCase):
    def test_only_registered_file_importers_are_callable(self):
        ci = ImporterFixture()
        self.assertEqual(set(import_methods(ci)), {"ImportFile", "ImportFilePlugin"})
        for command in ("ImportString", "__getattribute__", "Save", "Export"):
            with self.assertRaises(BridgeError):
                import_arguments(ci, command, "input.dat", {})

    def test_native_parameters_and_filename_position_are_preserved(self):
        method, kwargs = import_arguments(ImporterFixture(), "ImportFilePlugin", "data.npy",
                                          {"plugin": "Numpy NPY", "name": "signal"})
        self.assertEqual(kwargs, {"plugin": "Numpy NPY", "name": "signal", "filename": "data.npy"})
        self.assertEqual(method.__name__, "ImportFilePlugin")

    def test_missing_options_and_filename_override_are_rejected(self):
        for options in ({}, {"descriptor": "x", "filename": "other.dat"},
                        {"descriptor": "x", "unexpected": True}):
            with self.assertRaises(BridgeError):
                import_arguments(ImporterFixture(), "ImportFile", "input.dat", options)

    def test_source_is_validated_and_hashed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.dat"
            path.write_bytes(b"1 2\n")
            result = prepare_source(path)
            self.assertEqual(result["bytes"], 4)
            self.assertEqual(len(result["sha256"]), 64)
            with self.assertRaises(BridgeError):
                prepare_source(folder)

    def test_new_export_format_does_not_require_a_code_whitelist(self):
        formats = [{"extension": "future", "available": True, "multipage": False}]
        options = export_options({"dpi": 300}, 0, 2, formats, ".future")
        self.assertEqual(options["page"], [0])

    def test_multipage_and_unavailable_formats_are_validated(self):
        formats = [{"extension": "pdf", "available": True, "multipage": True},
                   {"extension": "png", "available": True, "multipage": False},
                   {"extension": "eps", "available": False, "multipage": False}]
        self.assertEqual(export_options({}, [0, 1], 2, formats, ".pdf")["page"], [0, 1])
        for pages, extension in (([0, 1], ".png"), ([2], ".pdf"), ([], ".pdf"), (0, ".eps")):
            with self.assertRaises(BridgeError):
                export_options({}, pages, 2, formats, extension)

    def test_export_parameters_are_typed_and_bounded(self):
        formats = [{"extension": "pdf", "available": True, "multipage": True}]
        for params in ({"dpi": 1}, {"options": {"filename": "x"}},
                       {"options": {"quality": 101}}, {"options": {"color": "yes"}}):
            with self.assertRaises(BridgeError):
                export_options(params, 0, 1, formats, ".pdf")


if __name__ == "__main__":
    unittest.main()
