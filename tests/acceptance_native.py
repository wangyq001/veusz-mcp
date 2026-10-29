"""Real MCP to Veusz acceptance; temporary edits are undone. Close modal dialogs first."""
import asyncio
import json
from pathlib import Path
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def connect(run):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "veusz_mcp.py")],
                                  cwd=str(ROOT), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            async def call(name, **args):
                if name not in ("veusz_status", "veusz_inspect", "veusz_widget_catalog", "veusz_operation"):
                    state = (await session.call_tool("veusz_status", {})).structuredContent
                    args.update(document_id=state["document_id"], revision=state["revision"])
                result = await session.call_tool(name, args)
                if result.isError:
                    raise RuntimeError(str(result.content))
                return result.structuredContent
            return await run(call, session)





async def run(call, session):
    checks = []
    baseline = await call("veusz_status")
    assert baseline["desktop_is_interactive"] and not baseline["modal"]
    assert baseline["window_class"] == "MainWindow" and all(baseline["editor_panels"].values())
    catalog = await call("veusz_widget_catalog")
    types = [v["type"] for v in catalog["widget_types"] if v["user_creatable"]]
    for kind in types:
        schema = await call("veusz_widget_catalog", widget_type=kind)
        assert schema["settings"], kind
    checks.append(dict(check="all_creatable_widget_schemas", passed=True, count=len(types)))
    data = [dict(name="qa_1d", data=[1,2,None], symerr=[.1,.2,None], negerr=[-.1,-.1,-.1]),
            dict(name="qa_2d", type="2d", data=[[1,2,3],[4,5,6]], xcent=[-1,0,2], yedge=[-2,0,3]),
            dict(name="qa_nd", type="nd", data=[[[1,2],[3,4]],[[5,6],[7,8]]]),
            dict(name="qa_text", type="text", data=["Alpha","中文"]),
            dict(name="qa_date", type="datetime", data=["2026-10-30T12:00:00+08:00","2026-10-30T05:00:00Z"])]
    result = await call("veusz_set_data", datasets=data)
    assert result["data_summary"]["qa_2d"]["shape"] == [2,3]
    assert result["data_summary"]["qa_nd"]["shape"] == [2,2,2]
    assert result["data_summary"]["qa_text"]["datatype"] == "text"
    raw = await session.call_tool("veusz_set_data", dict(datasets=data, document_id=result["document_id"], revision=result["revision"]))
    assert raw.isError and "DATA_EXISTS" in str(raw.content)
    restored = await call("veusz_undo")
    assert restored["datasets"] == baseline["datasets"]
    checks.append(dict(check="all_five_native_dataset_types_and_undo",passed=True))
    for bad in [dict(name="bad", type="2d", data=[[1,2],[3]], xrange=[0,1]),
                dict(name="bad", type="2d", data=[[1,2]], xcent=[0]),
                dict(name="bad", data=[1,2], symerr=[-.1,.1]),
                dict(name="bad", type="datetime", data=["not a date"])]:
        s=await call("veusz_status")
        raw=await session.call_tool("veusz_set_data", dict(datasets=[bad],document_id=s["document_id"],revision=s["revision"]))
        assert raw.isError and "INVALID_DATA" in str(raw.content), str(raw.content)
        assert (await call("veusz_status"))["datasets"] == baseline["datasets"]
    checks.append(dict(check="invalid_dimensions_coordinates_errors_and_dates",passed=True))
    p="/mcp_coverage"
    entries=[dict(type="page",parent="/",name="mcp_coverage",settings={"hide":True}),
             dict(type="grid",parent=p,name="layout"),
             dict(type="graph",parent=p+"/layout",name="cart"),
             dict(type="polar",parent=p,name="polar"),dict(type="ternary",parent=p,name="ternary"),
             dict(type="scene3d",parent=p,name="scene",autoadd=False),
             dict(type="graph3d",parent=p+"/scene",name="space")]
    infrastructure={e["type"] for e in entries}
    for item in catalog["widget_types"]:
        kind=item["type"]
        if not item["user_creatable"] or kind in infrastructure: continue
        parents=item["allowed_parents"]
        parent=p+"/scene/space" if "graph3d" in parents else p+"/layout/cart" if "graph" in parents else p if "page" in parents else p+"/polar"
        entries.append(dict(type=kind,parent=parent,name="qa_"+kind))
    result=await call("veusz_add_widgets",widgets=entries)
    created={w["type"] for w in result["widgets"] if w["path"].startswith(p)}
    assert set(types) <= created, set(types)-created
    # Read settings for every constructed native type, not only the factory schema.
    for w in result["widgets"]:
        if w["path"].startswith(p): await call("veusz_inspect",settings_group=w["path"])
    restored=await call("veusz_undo")
    assert restored["widgets"] == baseline["widgets"]
    checks.append(dict(check="all_creatable_types_native_creation_inspection_and_undo",passed=True,count=len(types)))
    before=await call("veusz_status")
    raw=await session.call_tool("veusz_add_widgets",dict(document_id=before["document_id"],revision=before["revision"],widgets=[
        dict(type="page",parent="/",name="rollback_probe"),
        dict(type="surface3d",parent="/rollback_probe",name="invalid_parent")]))
    assert raw.isError and "BAD_PARENT" in str(raw.content)
    after=await call("veusz_status")
    assert before["widgets"] == after["widgets"] and before["datasets"] == after["datasets"]
    checks.append(dict(check="partially_created_batch_failure_rolls_back",passed=True))
    raw=await session.call_tool("veusz_set_data",dict(document_id=before["document_id"],revision=-1,datasets=[dict(name="stale",data=[1])]))
    assert raw.isError and "REVISION_CONFLICT" in str(raw.content)
    checks.append(dict(check="new_data_api_revision_guard",passed=True))
    report=dict(veusz=baseline["veusz_version"],registered=len(catalog["widget_types"]),creatable=len(types),checks=checks)
    (ROOT / "runtime" / "native_live_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps(report,ensure_ascii=False))


asyncio.run(connect(run))
