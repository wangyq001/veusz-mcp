"""Typed access to Veusz's installed native file interfaces."""

import hashlib
import inspect
from pathlib import Path

from .native import json_value
from .protocol import BridgeError


def import_methods(ci):
    result = {}
    for name in ci.import_commands:
        index = ci.import_filenamearg.get(name, -1)
        if not name.startswith("Import") or not isinstance(index, int) or index < 0:
            continue
        method = getattr(ci, name)
        signature = inspect.signature(method)
        parameters = list(signature.parameters)
        if index < len(parameters):
            result[name] = (method, signature, parameters[index])
    return result


def import_arguments(ci, command, path, options):
    methods = import_methods(ci)
    if command not in methods:
        raise BridgeError("BAD_IMPORTER", "Choose a native file importer from veusz_file_formats")
    method, signature, filename = methods[command]
    if not isinstance(options, dict) or filename in options:
        raise BridgeError("BAD_REQUEST", "options must be an object without the filename parameter")
    kwargs = dict(options, **{filename: str(path)})
    try:
        signature.bind(**kwargs)
    except TypeError as exc:
        raise BridgeError("BAD_REQUEST", str(exc)) from exc
    return method, kwargs


def prepare_source(path):
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise BridgeError("BAD_PATH", "Input must be an existing local file")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"source": str(source), "sha256": digest.hexdigest(), "bytes": source.stat().st_size}


def hdf5_nd_compatibility(doc, path):
    """Encode ND arrays with native text commands: Veusz 4.2.1's HDF5 loader lacks an ND reader.

    The container remains an ordinary Veusz HDF5 project and opens without this bridge installed.
    """
    from io import StringIO
    import h5py

    nd = {name: ds for name, ds in doc.data.items() if ds.datatype == "numeric" and ds.dimensions not in (1, 2)}
    # DatasetND can also contain a 1D or 2D array, so inspect the stored native type as well.
    with h5py.File(path, "r+") as file:
        group = file["Veusz/Data"]
        encoded_names = []
        for encoded in list(group):
            kind = group[encoded].attrs.get("vsz_datatype")
            if kind in ("nd", b"nd"):
                from veusz import utils
                name = utils.unescapeHDFDataName(encoded)
                nd[name] = doc.data[name]
                encoded_names.append(encoded)
        if not encoded_names:
            return
        extra = StringIO()
        for name, ds in nd.items():
            ds.saveToFile(extra, name, mode="text")
            for tag in ds.tags:
                extra.write("TagDatasets(%r, [%r])\n" % (tag, name))
        for encoded in encoded_names:
            del group[encoded]
        tags = file["Veusz/Document/Tags"]
        for tag in list(tags):
            values = [v for v in tags[tag] if v.decode("utf-8") not in nd]
            del tags[tag]
            tags.create_dataset(tag, data=values, dtype=h5py.string_dtype("utf-8"))
        script = file["Veusz/Document/document"][0].decode("utf-8") + "\n" + extra.getvalue()
        del file["Veusz/Document/document"]
        file["Veusz/Document"].create_dataset("document", data=[script.encode("utf-8")])


def format_catalog(ci):
    from veusz import document, plugins, qtall as qt
    from veusz.document import export, doc

    exports = []
    writers = {bytes(v).decode("ascii").lower() for v in qt.QImageWriter.supportedImageFormats()}
    for extensions, description in export.AsyncExport.getFormats():
        for extension in extensions:
            raster = extension in {"bmp", "jpg", "png", "tiff", "xpm", "webp"}
            available = not raster or extension in writers
            exports.append({"extension": extension, "description": str(description),
                            "available": available,
                            "reason": None if available else "Qt image writer is unavailable",
                            "multipage": extension in ("pdf", "ps")})
    if any(item["extension"] == "jpg" and item["available"] for item in exports):
        exports.append({"extension": "jpeg", "description": "JPEG alias", "available": True,
                        "reason": None, "multipage": False})
    for extension in ("eps", "ps"):
        if not any(item["extension"] == extension for item in exports):
            exports.append({"extension": extension, "description": "PostScript", "available": False,
                            "reason": "Ghostscript or its required output device is unavailable",
                            "multipage": extension == "ps"})
    imports = []
    for name, (_, signature, filename) in sorted(import_methods(ci).items()):
        parameters = []
        for p in signature.parameters.values():
            if p.name == filename:
                continue
            item = {"name": p.name, "required": p.default is inspect.Parameter.empty and
                    p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL), "kind": str(p.kind)}
            if p.default is not inspect.Parameter.empty:
                item["default"] = json_value(p.default)
            parameters.append(item)
        dependency = "h5py" if "HDF5" in name else "astropy.io.fits" if "FITS" in name else None
        reason = None
        if dependency:
            try:
                __import__(dependency)
            except ImportError as exc:
                reason = str(exc)
        imports.append({"command": name, "filename_parameter": filename,
                        "parameters": parameters, "available": reason is None, "reason": reason,
                        "description": inspect.getdoc(getattr(ci, name))})
    plugin_info = []
    for cls in getattr(plugins, "importpluginregistry", []):
        try:
            instance = cls()
            plugin_info.append({"name": instance.name,
                                "extensions": list(getattr(instance, "file_extensions", [])),
                                "fields": [{"name": f.name, "type": type(f).__name__,
                                            "default": json_value(getattr(f, "default", None)),
                                            "choices": json_value(getattr(f, "items", None))}
                                           for f in getattr(instance, "fields", [])]})
        except Exception as exc:
            plugin_info.append({"name": getattr(cls, "name", cls.__name__), "reason": str(exc)})
    return {"exports": exports, "imports": imports, "import_plugins": plugin_info,
            "dependencies": {"ghostscript": getattr(export.ExportPostscriptRunnable, "gs_exe", None),
                             "postscript_available": bool(getattr(export.ExportPostscriptRunnable, "gs_dev", {})),
                             "emf_available": bool(getattr(export, "hasemf", False))},
            "projects": [{"extension": "vsz", "available": True},
                         {"extension": "vszh5", "available": doc.h5py is not None,
                          "reason": None if doc.h5py is not None else "h5py is not installed"}],
            "export_parameters": [p for p in inspect.signature(ci.Export).parameters if p != "filename"]}


def export_options(params, pages, count, supported, suffix):
    entry = next((e for e in supported if e["extension"] == suffix.lstrip(".")), None)
    if not entry or not entry["available"]:
        raise BridgeError("FORMAT_UNAVAILABLE", "Format is unavailable; inspect veusz_file_formats")
    if type(pages) is int:
        pages = [pages]
    if not isinstance(pages, list) or not pages or any(type(p) is not int or not 0 <= p < count for p in pages):
        raise BridgeError("BAD_PAGE", "Provide valid zero-based page numbers")
    if len(pages) > 1 and not entry["multipage"]:
        raise BridgeError("BAD_PAGE", "This format supports only one page")
    options = dict(params.get("options") or {})
    allowed = {"color", "antialias", "quality", "pdfdpi", "svgdpi", "svgtextastext"}
    if set(options) - allowed:
        raise BridgeError("BAD_REQUEST", "Unsupported export option")
    for key in ("color", "antialias", "svgtextastext"):
        if key in options and type(options[key]) is not bool:
            raise BridgeError("BAD_REQUEST", key + " must be boolean")
    for key, low, high in (("quality", 0, 100), ("pdfdpi", 50, 2400), ("svgdpi", 1, 2400)):
        if key in options and (type(options[key]) is not int or not low <= options[key] <= high):
            raise BridgeError("BAD_REQUEST", "Invalid " + key)
    dpi = params.get("dpi", 300)
    if type(dpi) is not int or not 50 <= dpi <= 1200:
        raise BridgeError("BAD_REQUEST", "DPI must be 50..1200")
    options.update(page=pages, dpi=dpi, backcolor=params.get("background", "#ffffffff"))
    return options
