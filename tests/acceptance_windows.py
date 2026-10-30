"""Real multi-editor MCP acceptance. Creates three test editors; never edits existing ones."""

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def connected(run):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "veusz_mcp.py")],
                                  cwd=str(ROOT), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            async def call(tool_name, window_id=None, **args):
                if window_id is not None:
                    args["window_id"] = window_id
                if tool_name not in ("veusz_windows", "veusz_new_window", "veusz_arrange_windows", "veusz_status",
                                "veusz_inspect", "veusz_file_formats"):
                    s = (await session.call_tool("veusz_status", {"window_id": window_id})).structuredContent
                    args.update(document_id=s["document_id"], revision=s["revision"])
                result = await session.call_tool(tool_name, args)
                if result.isError:
                    raise RuntimeError(str(result.content))
                return result.structuredContent
            return await run(call, session)


async def main():
    run_id = uuid.uuid4().hex[:10]
    folder = ROOT / "output" / ("qa-windows-" + run_id)
    folder.mkdir(parents=True, exist_ok=False)
    report = {"windows": [], "checks": [], "directory": str(folder)}

    async def create_and_check(call, session):
        baseline = await call("veusz_windows")
        report["preexisting_windows"] = baseline["windows"]
        for i, label in enumerate(("Figure A - line", "Figure B - scatter", "Figure C - bars")):
            request_id = "qa-window-" + run_id + "-" + str(i)
            created = await call("veusz_new_window", name=label, request_id=request_id)
            identity = created["window_id"]
            state = created["state"]
            assert state["desktop_is_interactive"] and state["window_native_visible"]
            if i == 0:
                retry = await call("veusz_new_window", name=label, request_id=request_id)
                assert retry["window_id"] == identity and retry["state"]["pid"] == state["pid"]
            await call("veusz_set_data", identity, datasets=[
                {"name": "x", "data": [1, 2, 3, 4]},
                {"name": "y", "data": [[2, 4, 3, 6], [6, 3, 5, 2], [3, 6, 2, 5]][i]}])
            if i < 2:
                await call("veusz_plot", identity, graph_path="/page1/graph1", series=[{
                    "name": "signal", "x": "x", "y": "y", "label": "Series " + "AB"[i],
                    "kind": "line" if i == 0 else "scatter", "color": "#0F4D92" if i == 0 else "#B64342"}])
            else:
                await call("veusz_add_widgets", identity, widgets=[{
                    "type": "bar", "parent": "/page1/graph1", "name": "signal",
                    "settings": {"lengths": ["y"], "posn": "x"}}])
            await call("veusz_add_widgets", identity, widgets=[{
                "type": "label", "parent": "/page1", "name": "title",
                "settings": {"label": label, "xPos": [0.5], "yPos": [0.94], "alignHorz": "centre",
                             "Text__size": "16pt", "Text__bold": True}}])
            await call("veusz_apply_changes", identity, changes=[
                {"path": "/page1/width", "value": "16cm"},
                {"path": "/page1/height", "value": "11cm"},
                {"path": "/page1/graph1/topMargin", "value": "1.2cm"},
                {"path": "/page1/graph1/x/label", "value": "Sample"},
                {"path": "/page1/graph1/y/label", "value": "Value"}])
            path = folder / ("figure-" + "abc"[i] + ".vsz")
            await call("veusz_save", identity, path=str(path))
            await call("veusz_export", identity, path=str(path.with_suffix(".png")), dpi=120)
            report["windows"].append({"window_id": identity, "pid": state["pid"],
                                       "hwnd": state["window_handle"], "file": str(path),
                                       "request_id": request_id})
        assert len({w["pid"] for w in report["windows"]}) == 3
        report["checks"].append("three_independent_real_desktop_editors")
        ids = [w["window_id"] for w in report["windows"]]
        before_b = await call("veusz_status", ids[1])
        before_c = await call("veusz_status", ids[2])
        await call("veusz_apply_changes", ids[0], changes=[{"path": "/page1/graph1/signal/key", "value": "A only edited"}])
        assert (await call("veusz_status", ids[1]))["revision"] == before_b["revision"]
        assert (await call("veusz_status", ids[2]))["revision"] == before_c["revision"]
        b = await call("veusz_inspect", ids[1], paths=["/page1/graph1/signal/key"])
        assert b["values"]["/page1/graph1/signal/key"] == "Series B"
        report["checks"].append("editing_a_preserves_b_and_c")
        a = await call("veusz_status", ids[0])
        wrong = await session.call_tool("veusz_apply_changes", {
            "window_id": ids[1], "document_id": a["document_id"], "revision": before_b["revision"],
            "changes": [{"path": "/page1/graph1/signal/key", "value": "must not happen"}]})
        assert wrong.isError and "DOCUMENT_CHANGED" in str(wrong.content)
        ambiguous = await session.call_tool("veusz_status", {})
        assert ambiguous.isError and "AMBIGUOUS_WINDOW" in str(ambiguous.content)
        report["checks"].append("cross_document_write_and_ambiguous_target_rejected")
        saved = await call("veusz_save", ids[0], path=report["windows"][0]["file"], overwrite=True)
        await call("veusz_export", ids[0], path=str(folder / "figure-a.png"), dpi=120)
        arranged = await call("veusz_arrange_windows", window_ids=ids, columns=2)
        assert arranged["complete"], arranged
        report["checks"].append("three_windows_tiled_and_pages_fitted")
        report["arrangement"] = arranged

    await connected(create_and_check)

    async def reconnected(call, session):
        listed = await call("veusz_windows")
        available = {w["window_id"]: w for w in listed["windows"] if w["available"]}
        for w in report["windows"]:
            assert available[w["window_id"]]["pid"] == w["pid"]
            s = await call("veusz_status", w["window_id"])
            assert s["filename"] == w["file"] and not s["modified"]
        report["checks"].append("mcp_restart_preserves_all_window_identities_and_files")
    await connected(reconnected)
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
