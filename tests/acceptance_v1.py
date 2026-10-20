"""Real SDK STDIO -> MCP server -> socket bridge -> native Veusz acceptance."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from veusz_bridge.client import Client
from bridge_cli import resolve_executable

SESSION = Path(os.environ.get("VEUSZ_SESSION_FILE", ROOT / "runtime/acceptance/session.json"))
PREFIX = os.environ.get("VEUSZ_ACCEPTANCE_PREFIX", "output/v0.1")
OUT = ROOT / PREFIX


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source = OUT / "原始示例数据.csv"
    # The upstream example contains two different tables separated by an empty
    # row. Select its first table, retaining the original symmetric errors.
    configured_example = os.environ.get("VEUSZ_EXAMPLE_CSV")
    original = Path(configured_example) if configured_example else resolve_executable().parent / "examples" / "example_csv.csv"
    source.write_text(original.read_text(encoding="utf-8-sig").split("\n,,,", 1)[0] + "\n", encoding="utf-8")
    report = {"checks": [], "human_edit": "pending", "source": str(source),
              "source_origin": str(original),
              "source_selection": "first table, before the blank separator row",
              "started_at": time.time()}
    env = dict(os.environ, VEUSZ_SESSION_FILE=str(SESSION), PYTHONIOENCODING="utf-8")
    server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "veusz_mcp.py")], env=env, cwd=str(ROOT))

    def passed(name, **detail):
        report["checks"].append({"name": name, "passed": True, **detail})
        print("PASS", name, flush=True)

    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as mcp:
            await mcp.initialize()

            async def call(name, **arguments):
                result = await mcp.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(" ".join(getattr(c, "text", "") for c in result.content))
                return result.structuredContent

            async def state():
                return await call("veusz_status")

            async def write(name, **arguments):
                s = await state()
                return await call(name, document_id=s["document_id"], revision=s["revision"], **arguments)

            async def error(code, name, **arguments):
                result = await mcp.call_tool(name, arguments)
                assert result.isError, "Expected MCP tool error " + code
                assert code in str(result.content), str(result.content)

            tools = await mcp.list_tools()
            assert len(tools.tools) == 13
            passed("official_sdk_stdio_initialization_and_tool_discovery", tools=len(tools.tools))
            s = await state()
            assert s["window_class"] == "MainWindow" and not s["modal"]
            assert all(s["editor_panels"].values())
            assert (s["window_handle"] > 0 and s["window_native_exists"] and
                    s["window_native_visible"] and not s["window_native_minimized"] and
                    s["window_native_on_screen"] and s["window_native_pid"] == s["pid"])
            assert (s["desktop_is_interactive"] and
                    s["thread_desktop"] == s["input_desktop"])
            assert not any(w["path"] == "/acceptance" for w in s["widgets"]), "Use a fresh acceptance document"
            report["environment"] = {k:s[k] for k in
                                      ("pid","window_handle","thread_desktop","input_desktop",
                                       "bridge_version","veusz_version","python","qt")}
            passed("independent_full_editor_connected")

            await write("veusz_import_data", path=str(source), columns={"x":"Xval", "y":"Yval", "err":"+-"})
            s = await state()
            assert s["data_summary"]["x"]["length"] == s["data_summary"]["y"]["length"]
            assert s["data_sources"][-1]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            passed("official_csv_unicode_path_import_and_source_hash")
            await write("veusz_plot", page_name="acceptance", x_label="X", y_label="Y",
                        series=[{"name":"measurement","x":"x","y":"y","error":"err","kind":"errorbar",
                                 "label":"Measured data","color":"#2563eb"},
                                {"name":"scatter","x":"x","y":"y","kind":"scatter",
                                 "label":"Scatter","color":"#c026d3"}])
            s = await state()
            assert "__measurement_with_error" in s["datasets"]
            passed("native_errorbar_and_scatter_plot")

            key = "/acceptance/graph/measurement/key"
            width = "/acceptance/graph/measurement/PlotLine/width"
            old = await state()
            await write("veusz_apply_changes", changes=[{"path":key,"value":"MCP updated"}, {"path":width,"value":"2pt"}])
            await error("REVISION_CONFLICT", "veusz_apply_changes", document_id=old["document_id"], revision=old["revision"],
                        changes=[{"path":key,"value":"stale overwrite"}])
            passed("stale_mcp_write_rejected")
            await write("veusz_undo")
            got = await call("veusz_inspect", paths=[key, width])
            assert got["values"][key] == "Measured data"
            assert got["values"][width] == "1.5pt"
            passed("mcp_batch_native_undo")

            before = await state()
            await write("veusz_checkpoint", path=PREFIX + "/recovery.vsz")
            after = await state()
            assert all(before[k] == after[k] for k in ("revision", "filename", "modified", "document_id"))
            passed("checkpoint_preserves_dirty_flag_filename_and_revision")
            await error("UNSAVED_CHANGES", "veusz_open", path=PREFIX + "/recovery.vsz",
                        document_id=after["document_id"], revision=after["revision"])
            passed("open_refuses_unsaved_changes")

            saved = await write("veusz_save", path=PREFIX + "/可编辑图形.vsz", overwrite=True)
            assert saved["state"]["modified"] is False
            old_id = saved["state"]["document_id"]
            await write("veusz_open", path=PREFIX + "/可编辑图形.vsz")
            reopened = await state()
            assert reopened["document_id"] != old_id
            got = await call("veusz_inspect", paths=[key, width])
            assert got["values"][key] == "Measured data"
            assert "__measurement_with_error" in reopened["datasets"]
            passed("editable_vsz_save_and_reopen")

            invalid = OUT / "invalid.vsz"
            invalid.write_text("this is not valid python !!", encoding="utf-8")
            before = await state()
            await error("OPEN_FAILED", "veusz_open", path=str(invalid), document_id=before["document_id"], revision=before["revision"])
            after = await state()
            assert before["document_id"] == after["document_id"] and before["widgets"] == after["widgets"]
            passed("invalid_open_preserves_current_document")

            for extension in ("png", "svg", "pdf"):
                await write("veusz_export", path=PREFIX + "/figure." + extension, dpi=300, page=1)
                assert (OUT / ("figure." + extension)).stat().st_size > 1000
            s = await state()
            preview = await mcp.call_tool("veusz_preview", {"document_id":s["document_id"], "revision":s["revision"]})
            assert not preview.isError and any(c.type == "image" for c in preview.content)
            passed("png_svg_pdf_and_mcp_image_preview")

            s = await state()
            args = dict(document_id=s["document_id"],revision=s["revision"],request_id="acceptance-write",
                        changes=[{"path":width,"value":"2pt"}])
            result = await call("veusz_apply_changes", **args)
            again = await call("veusz_apply_changes", **args)
            assert result == again
            status = await call("veusz_operation", request_id="acceptance-write")
            assert status["state"] == "done"
            passed("mcp_write_dedup_and_operation_query")

            # At least 1,000 mixed real bridge requests; writes are journalled.
            client = Client(SESSION)
            initial = client.state()
            latencies, render_latencies = [], []
            for index in range(1000):
                if index % 20:
                    current = client.state()
                    assert current["pid"] == initial["pid"]
                else:
                    current = client.state()
                    start = time.perf_counter()
                    current = client.call("set", {"changes":[{"path":width,"value":"1.5pt" if index % 40 else "2pt"}]}, state=current)
                    latencies.append((time.perf_counter()-start)*1000)
                    deadline = time.monotonic()+3
                    while time.monotonic() < deadline:
                        rendered = client.state().get("rendered")
                        if rendered and rendered["revision"] == current["revision"]:
                            render_latencies.append((time.perf_counter()-start)*1000)
                            break
                        await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("Native rendering did not finish")
            p95 = lambda items: sorted(items)[int(len(items)*.95)-1]
            passed("1000_mixed_real_requests", mutations=len(latencies),
                   commit_p95_ms=round(p95(latencies),2), render_p95_ms=round(p95(render_latencies),2))
            await write("veusz_save", path=PREFIX + "/可编辑图形.vsz", overwrite=True)
            report["manual_baseline"] = await state()
            report["automated_passed"] = True
    # A fresh MCP server process must reconnect to the same running GUI.
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            result = await mcp.call_tool("veusz_status")
            assert not result.isError
            assert result.structuredContent["pid"] == report["environment"]["pid"]
            passed("mcp_process_restart_preserves_gui_and_document")
    (OUT / "verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Acceptance complete; human edit pending", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
