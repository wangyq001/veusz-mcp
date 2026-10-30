"""Real native I/O acceptance. Requires a dedicated disposable bridge session."""

import asyncio
import json
import os
from pathlib import Path
import re
import struct
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def fixtures(folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "text.dat").write_text("1 2\n2 4\n3 3\n", encoding="utf-8")
    (folder / "columns.csv").write_text("csv_x,csv_y\n1,2\n2,4\n3,3\n", encoding="utf-8")
    (folder / "matrix.dat").write_text("1 2\n3 4\n", encoding="utf-8")
    (folder / "gnuplot.data").write_text("0 0 1\n1 0 2\n\n0 1 3\n1 1 4\n", encoding="ascii")
    header = repr({"descr": "<f8", "fortran_order": False, "shape": (3,)})
    header += " " * ((64 - (10 + len(header) + 1) % 64) % 64) + "\n"
    npy = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header.encode() + struct.pack("<3d", 1, 2, 3)
    (folder / "array.npy").write_bytes(npy)
    with zipfile.ZipFile(folder / "arrays.npz", "w") as archive:
        archive.writestr("npz_signal.npy", npy)
    (folder / "binary.bin").write_bytes(struct.pack("<3d", 1, 2, 3))
    (folder / "sample.qdp").write_text("1 2\n2 4\n3 3\n", encoding="ascii")
    cards = [("SIMPLE", "T"), ("BITPIX", "16"), ("NAXIS", "2"), ("NAXIS1", "2"), ("NAXIS2", "2")]
    fits = "".join((name.ljust(8) + "= " + value.rjust(20)).ljust(80) for name, value in cards)
    fits += "END".ljust(80)
    fits = fits.ljust((len(fits) + 2879) // 2880 * 2880)
    data = struct.pack(">4h", 1, 2, 3, 4)
    (folder / "image.fits").write_bytes(fits.encode("ascii") + data.ljust(2880, b"\0"))
    (folder / "table.xml").write_text(
        '<?xml version="1.0"?><VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">'
        '<RESOURCE><TABLE><FIELD name="vo_x" datatype="double"/><FIELD name="vo_y" datatype="double"/>'
        '<DATA><TABLEDATA><TR><TD>1</TD><TD>2</TD></TR><TR><TD>2</TD><TD>4</TD></TR>'
        '</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>', encoding="utf-8")


async def main():
    if not os.environ.get("VEUSZ_SESSION_FILE"):
        raise RuntimeError("Set VEUSZ_SESSION_FILE to a dedicated test session; never use a work document")
    folder = ROOT / "output" / "qa-file-formats"
    fixtures(folder)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "veusz_mcp.py")],
                                  cwd=str(ROOT), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    report = {"imports": [], "exports": [], "projects": [], "failures": []}
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            async def call(name, **args):
                if name not in ("veusz_status", "veusz_file_formats"):
                    s = (await session.call_tool("veusz_status", {})).structuredContent
                    args.update(document_id=s["document_id"], revision=s["revision"])
                result = await session.call_tool(name, args)
                if result.isError:
                    raise RuntimeError(str(result.content))
                return result.structuredContent
            initial = await call("veusz_status")
            assert not initial["modified"] and not initial["datasets"], "Use a blank test window"
            catalog = await call("veusz_file_formats")
            report["capabilities"] = catalog
            async def imported(command, file, options):
                try:
                    r = await call("veusz_import_file", path=str(folder / file), command=command, options=options)
                    assert r["datasets"]
                    report["imports"].append({"command": command, "file": file, "passed": True})
                except Exception as exc:
                    report["failures"].append({"stage": "import", "command": command, "file": file, "error": str(exc)})
            await imported("ImportFile", "text.dat", {"descriptor": "tx ty"})
            await imported("ImportFileCSV", "columns.csv", {})
            await imported("ImportFile2D", "matrix.dat", {"datasetnames": ["matrix"]})
            await imported("ImportFileND", "matrix.dat", {"dataset": "nd_matrix", "shape": [2, 2]})
            await imported("ImportFileFITS", "image.fits", {"items": ["/"], "prefix": "fits_"})
            await imported("ImportFITSFile", "image.fits", {"dsname": "legacy_fits", "hdu": 0})
            for plugin in catalog["import_plugins"]:
                name = plugin["name"]
                cases = {
                    "Numpy NPY import": ("array.npy", {"name": "npy_signal"}),
                    "Numpy NPZ import": ("arrays.npz", {}),
                    "QDP import": ("sample.qdp", {"names": ["qdp_x", "qdp_y"]}),
                    "Binary import": ("binary.bin", {"name": "bin_signal", "datatype": "float64", "endian": "little"}),
                    "Example plugin": ("text.dat", {"name": "example_signal"}),
                    "Gnuplot 2D data import plugin": ("gnuplot.data", {"name": "gnuplot_matrix"}),
                    "VO table import": ("table.xml", {})}
                if name in cases:
                    file, opts = cases[name]
                    await imported("ImportFilePlugin", file, {"plugin": name, **opts})
            await call("veusz_plot", graph_path="/page1/graph1", series=[{"x": "tx", "y": "ty", "name": "signal"}])
            await call("veusz_add_widgets", widgets=[{"type": "page", "parent": "/", "name": "second"},
                {"type": "label", "parent": "/second", "name": "title", "settings": {"label": "Multipage test"}}])
            for fmt in catalog["exports"]:
                if not fmt["available"]:
                    continue
                extension = fmt["extension"]
                try:
                    path = folder / ("native." + extension)
                    await call("veusz_export", path=str(path), page=0, dpi=100)
                    assert path.stat().st_size > 10
                    report["exports"].append({"extension": extension, "passed": True, "bytes": path.stat().st_size})
                except Exception as exc:
                    report["failures"].append({"stage": "export", "extension": extension, "error": str(exc)})
            await call("veusz_export", path=str(folder / "multipage.pdf"), pages=[0, 1])
            pdf = (folder / "multipage.pdf").read_bytes()
            assert len(re.findall(rb"/Type\s*/Page\b", pdf)) == 2
            for suffix in ("vsz", "vszh5"):
                try:
                    path = folder / ("roundtrip." + suffix)
                    saved = await call("veusz_save", path=str(path), overwrite=True)
                    before = saved["state"]["data_summary"]
                    opened = await call("veusz_open", path=str(path))
                    assert before == opened["data_summary"]
                    report["projects"].append({"extension": suffix, "passed": True})
                except Exception as exc:
                    report["failures"].append({"stage": "project", "extension": suffix, "error": str(exc)})
            await imported("ImportFileHDF5", "roundtrip.vszh5", {"items": ["/Veusz/Data/tx/data"], "prefix": "h5_"})
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "capabilities"}, ensure_ascii=True))
    assert not report["failures"], "Inspect output/qa-file-formats/report.json"


if __name__ == "__main__":
    asyncio.run(main())
