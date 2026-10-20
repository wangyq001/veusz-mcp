"""Strict numeric CSV/TSV parsing, performed outside the Qt main thread."""

import csv
import hashlib
import math
from pathlib import Path

from .protocol import BridgeError


def read_table(filename, columns, delimiter=None, missing="error", encoding="utf-8-sig"):
    path = Path(filename).resolve()
    if path.suffix.lower() not in (".csv", ".tsv") or not path.is_file():
        raise BridgeError("BAD_DATA_FILE", "Choose an existing CSV or TSV file")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise BridgeError("DATA_LIMIT", "v0.1 supports source files up to 50 MiB")
    if not isinstance(columns, dict) or not 1 <= len(columns) <= 32:
        raise BridgeError("BAD_COLUMNS", "Map 1..32 dataset names to column headers")
    if missing not in ("error", "drop_rows"):
        raise BridgeError("BAD_REQUEST", "missing must be error or drop_rows")
    for name, header in columns.items():
        if not isinstance(name, str) or not name or not isinstance(header, str) or not header:
            raise BridgeError("BAD_COLUMNS", "Dataset names and column headers must be nonempty strings")
    raw = path.read_bytes()
    text = raw.decode(encoding)
    separator = delimiter or ("\t" if path.suffix.lower() == ".tsv" else ",")
    if separator not in (",", "\t", ";"):
        raise BridgeError("BAD_REQUEST", "Supported delimiters are comma, tab and semicolon")
    # splitlines preserves CSV's quoted multiline fields through csv.reader.
    import io
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=separator)
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise BridgeError("BAD_COLUMNS", "Duplicate headers are ambiguous")
    absent = set(columns.values()) - set(headers)
    if absent:
        raise BridgeError("BAD_COLUMNS", "Missing headers: " + ", ".join(sorted(absent)))
    datasets = {name: [] for name in columns}
    dropped = 0
    for number, row in enumerate(reader, start=2):
        if number > 1000001:
            raise BridgeError("DATA_LIMIT", "v0.1 supports at most 1,000,000 rows")
        try:
            if None in row:
                raise ValueError("too many columns")
            converted = {name: float(row[header]) for name, header in columns.items()}
            if not all(math.isfinite(value) for value in converted.values()):
                raise ValueError("nonfinite")
        except (ValueError, TypeError):
            if missing == "drop_rows":
                dropped += 1
                continue
            raise BridgeError("INVALID_DATA", "Invalid or missing numeric value at CSV row %d" % number)
        for name, value in converted.items():
            datasets[name].append(value)
        if len(next(iter(datasets.values()))) * len(columns) > 2000000:
            raise BridgeError("DATA_LIMIT", "Selected data is limited to 2,000,000 numeric values")
    if not any(datasets.values()):
        raise BridgeError("INVALID_DATA", "No numeric rows remain")
    return {"datasets": datasets, "source": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "rows": len(next(iter(datasets.values()))), "dropped_rows": dropped,
            "columns": columns}
