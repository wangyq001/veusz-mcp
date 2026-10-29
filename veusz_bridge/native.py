"""Runtime-native widget discovery and typed data preparation; no script execution."""

import math
from datetime import datetime, timezone

from .protocol import BridgeError


DATA_TYPES = ("1d", "2d", "nd", "text", "datetime")


def json_value(value):
    """Keep native defaults/references and numpy values valid in strict JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if hasattr(value, "tolist"):
        return json_value(value.tolist())
    if hasattr(value, "value"):
        return {"reference": json_value(value.value)}
    return str(value)


def setting_info(child, path, current=False):
    item = {"name": child.name, "path": path, "group": child.issettings,
            "description": child.descr, "type": getattr(child, "typename", "group")}
    if child.issettings:
        item["value"] = None
    else:
        item.update(default=json_value(child.default), readonly=child.readonly)
        for key in ("vallist", "minval", "maxval", "dimensions", "datatype"):
            if hasattr(child, key):
                item["choices" if key == "vallist" else key] = json_value(getattr(child, key))
        if current:
            item["value"] = json_value(child.val)
            if child.isReference():
                item["reference"] = json_value(child.getReference().value)
    return item


def widget_catalog(doc, widget_type=None, parent_path=None):
    from veusz import document, setting

    factory = document.thefactory
    parent = doc.resolveWidgetPath(doc.basewidget, parent_path) if parent_path else None
    items = []
    for name in factory.listWidgets():
        cls = factory.getWidgetClass(name)
        parents = cls.allowedParentTypes()
        item = {"type": name, "description": str(getattr(cls, "description", name)),
                "allowed_parents": [p.typename for p in parents if p is not None],
                "user_creatable": bool(cls.allowusercreation)}
        if parent is not None:
            item["allowed_here"] = cls.willAllowParent(parent)
        items.append(item)
    result = {"widget_types": items, "dataset_types": list(DATA_TYPES),
              "veusz_version": __import__("veusz.utils", fromlist=["version"]).version()}
    if widget_type is not None:
        if widget_type not in factory.listWidgets():
            raise BridgeError("BAD_WIDGET", "Unknown registered widget type: " + str(widget_type))
        cls = factory.getWidgetClass(widget_type)
        settings = setting.Settings("schema")
        cls.addSettings(settings)
        cls.addSettingsCompatLevel(settings, doc.compatlevel)
        schema = []
        def visit(group, prefix):
            for child in group.getList():
                path = prefix + child.name
                schema.append(setting_info(child, path))
                if child.issettings:
                    visit(child, path + "/")
        visit(settings, "")
        result.update(widget_type=widget_type, settings=schema)
    return result


def validate_widgets(entries, factory):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise BridgeError("BAD_REQUEST", "Provide 1..100 widgets")
    registered = set(factory.listWidgets())
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") not in registered:
            raise BridgeError("BAD_WIDGET", "Use a registered type from veusz_widget_catalog")
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name:
            raise BridgeError("BAD_WIDGET", "A nonempty widget name without slashes is required")
        parent = entry.get("parent", "/")
        if not isinstance(parent, str) or not parent.startswith("/"):
            raise BridgeError("BAD_WIDGET", "Parent paths must be absolute")
        if not isinstance(entry.get("settings", {}), dict):
            raise BridgeError("BAD_WIDGET", "settings must be an object")
        if type(entry.get("autoadd", True)) is not bool:
            raise BridgeError("BAD_WIDGET", "autoadd must be a boolean")
        if set(entry.get("settings", {})) & {"name", "widget", "autoadd", "index"}:
            raise BridgeError("BAD_WIDGET", "Use entry fields for name, parent and autoadd")


def prepare_datasets(entries):
    """Validate all arrays before touching a document. Null is a numeric gap (NaN)."""
    import numpy as np

    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise BridgeError("INVALID_DATA", "Provide 1..100 datasets")
    prepared, names = [], set()
    def numeric(values, ndim=None):
        try:
            array = np.asarray(values, dtype=float)
        except (ValueError, TypeError):
            raise BridgeError("INVALID_DATA", "Numeric data must be rectangular numeric arrays")
        if array.ndim < 1 or not array.size or ndim is not None and array.ndim != ndim:
            raise BridgeError("INVALID_DATA", "Dataset dimensions/shape are invalid")
        if np.isinf(array).any():
            raise BridgeError("INVALID_DATA", "Infinity is not supported; use null for gaps")
        return array
    for entry in entries:
        if not isinstance(entry, dict):
            raise BridgeError("INVALID_DATA", "Dataset entries must be objects")
        name, kind = entry.get("name"), entry.get("type", "1d")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise BridgeError("INVALID_DATA", "Dataset names must be nonempty and unique")
        names.add(name)
        if kind not in DATA_TYPES:
            raise BridgeError("INVALID_DATA", "Dataset type must be " + ", ".join(DATA_TYPES))
        data = entry.get("data")
        kwargs = {k: v for k, v in entry.items() if k not in ("name", "type", "data")}
        allowed = {"1d": {"symerr", "negerr", "poserr"},
                   "2d": {"xrange", "yrange", "xedge", "yedge", "xcent", "ycent"},
                   "nd": set(), "text": set(), "datetime": set()}[kind]
        if set(kwargs) - allowed:
            raise BridgeError("INVALID_DATA", "Unsupported dataset options: " + str(sorted(set(kwargs) - allowed)))
        if kind in ("text", "datetime"):
            if not isinstance(data, list) or not data or not all(isinstance(v, str) for v in data):
                raise BridgeError("INVALID_DATA", "Text/date data must be nonempty lists of strings")
            if kind == "datetime":
                try:
                    data = [datetime.fromisoformat(v.replace("Z", "+00:00")) for v in data]
                    data = [v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v for v in data]
                except ValueError:
                    raise BridgeError("INVALID_DATA", "Dates must be ISO 8601 strings")
        else:
            data = numeric(data, {"1d": 1, "2d": 2}.get(kind))
            for key, value in list(kwargs.items()):
                if value is None:
                    del kwargs[key]
                    continue
                value = numeric(value, 1)
                if kind == "1d":
                    if value.shape != data.shape:
                        raise BridgeError("INVALID_DATA", "Error arrays must match the data length")
                    if key in ("symerr", "poserr") and np.any(value < 0) or key == "negerr" and np.any(value > 0):
                        raise BridgeError("INVALID_DATA", "symerr/poserr must be >=0 and negerr <=0")
                elif not np.isfinite(value).all():
                    raise BridgeError("INVALID_DATA", "Grid coordinates must be finite")
                kwargs[key] = value
            if kind == "2d":
                for axis, size in (("x", data.shape[1]), ("y", data.shape[0])):
                    keys = [axis + suffix for suffix in ("range", "edge", "cent") if axis + suffix in kwargs]
                    if len(keys) > 1:
                        raise BridgeError("INVALID_DATA", "Choose range, edge or cent per axis")
                    for key in keys:
                        arr = kwargs[key]
                        expected = 2 if key.endswith("range") else size + int(key.endswith("edge"))
                        if len(arr) != expected or not np.all(np.diff(arr) > 0):
                            raise BridgeError("INVALID_DATA", "Grid coordinates must be ascending with the correct length")
        prepared.append((name, kind, data, kwargs))
    return prepared
