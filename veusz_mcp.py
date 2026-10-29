"""Veusz MCP v0.1: typed tools over the official SDK's STDIO transport."""

import os
from pathlib import Path
import time
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from veusz_bridge.client import Client, RemoteError

PLOTTING_GUIDANCE = (
    "Render final plots requested through this server as native editable Veusz objects. "
    "Plotting Skills are optional and chosen by the user or client, never bound to this server. "
    "If a Skill is supplied, read its relevant references and translate applicable design guidance "
    "(fonts, sizes, colors, layout and annotations) into Veusz settings. "
    "Explicit user requirements take priority over Skill defaults; preserve existing manual edits. "
    "Without a Skill, plot normally without requiring one. Do not assume, scan for or install Skills. "
    "Code may compute data, but do not execute a Skill's plotting backend or embed a finished image "
    "as a substitute for native plotting. Report unsupported effects instead of silently switching backends."
)

mcp = FastMCP("veusz", instructions=(
    "Control the full native Veusz editor through its document API, without mouse automation. "
    "Read veusz_status before editing. Discover all installed native widget types and their "
    "settings with veusz_widget_catalog; use veusz_add_widgets for any registered type, "
    "and veusz_set_data for 1d, 2d, nd, text and datetime data. "
    "Pass the returned document_id and revision explicitly. "
    "On REVISION_CONFLICT inspect the human's latest edits; never blindly retry. "
    "Keep request_id for uncertain operations and query veusz_operation. "
    "Never close, kill or restart the user's GUI. Start it with start_veusz.cmd if disconnected. "
    "Launch GUI processes only from the user's interactive desktop using approved desktop execution; "
    "a sandbox desktop is not a visible user window. Verify desktop_is_interactive is true before "
    "reporting that the window is displayed; window_visible alone is insufficient. "
    + PLOTTING_GUIDANCE))

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def invoke(method, params=None, document_id=None, revision=None, request_id=None):
    rid = request_id or str(uuid.uuid4())
    try:
        client = Client()
        if client.session.get("bridge_version") != "0.1.0":
            raise ToolError("BRIDGE_VERSION: start the v0.1 GUI using start_veusz.cmd")
        state = {"document_id": document_id, "revision": revision} if document_id is not None else None
        result = client.call(method, params, state=state, request_id=rid, timeout_ms=30000)
        return result
    except (OSError, ValueError, RemoteError) as exc:
        raise ToolError("%s; request_id=%s. Inspect status/result before retrying writes." % (exc, rid)) from exc


@mcp.tool(annotations=READ)
def veusz_status() -> dict[str, Any]:
    """Read document state and plotting_workflow: native editable output, with an optional user/client Skill."""
    state = invoke("state")
    return {**state, "plotting_workflow": {
        "output_backend": "veusz_native",
        "editable_output": True,
        "skill_optional": True,
        "skill_selection": "user_or_client",
        "bound_skill": None,
        "guidance": PLOTTING_GUIDANCE,
    }}


@mcp.tool(annotations=READ)
def veusz_inspect(paths: list[str] | None = None, settings_group: str | None = None) -> dict[str, Any]:
    """Read absolute settings, or list a group's child settings, values, types, choices and widget actions."""
    if settings_group:
        return invoke("describe", {"path": settings_group})
    return invoke("get", {"paths": paths or []})


@mcp.tool(annotations=READ)
def veusz_widget_catalog(widget_type: str | None = None, parent_path: str | None = None) -> dict[str, Any]:
    """Discover ALL widget types registered in the running Veusz, including 3D and installed plugins.
    Returns legal parent types and supported dataset kinds. Supply widget_type for its full recursive
    settings schema (relative paths, defaults, choices, ranges and dataset dimensions).
    Optional absolute parent_path marks which types can be placed there. Discovery never edits the document.
    """
    return invoke("widget_catalog", dict(widget_type=widget_type, parent_path=parent_path))


@mcp.tool(annotations=WRITE)
def veusz_set_data(datasets: list[dict], document_id: str, revision: int, replace: bool = False,
                   request_id: str | None = None) -> dict[str, Any]:
    """Set native datasets as one undoable batch. Entries: {name,type,data,...options}.
    type: 1d (default), 2d (row-major matrix), nd, text, datetime (ISO 8601 strings; offsets convert to UTC).
    1d options: symerr/poserr >=0, negerr <=0; arrays match data length.
    2d options: xrange/yrange=[min,max], or xedge/yedge (size+1), or xcent/ycent (size), ascending.
    Choose one coordinate form per axis. Numeric nulls represent missing values (NaN).
    Existing names require replace=true. No Python execution or arbitrary command dispatch.
    """
    return invoke("set_data", dict(datasets=datasets, replace=replace), document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_widget_action(path: str, action: str, document_id: str, revision: int,
                        request_id: str | None = None) -> dict[str, Any]:
    """Run a native widget's registered action (e.g. fit); discover actions with veusz_inspect.
    Absolute widget path required. Native operations are grouped for undo; no arbitrary Python.
    """
    return invoke("widget_action", dict(path=path, action=action), document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_import_data(path: str, columns: dict[str, str], document_id: str, revision: int,
                      missing: str = "error", delimiter: str | None = None,
                      encoding: str = "utf-8-sig", replace: bool = False,
                      request_id: str | None = None) -> dict[str, Any]:
    """Import numeric CSV/TSV. columns maps dataset names to headers. missing='error' or 'drop_rows'.
    Drop rows consistently across selected columns. Does not change units. Existing datasets require replace=true.
    """
    return invoke("import_data", dict(path=path, columns=columns, missing=missing,
                  delimiter=delimiter, encoding=encoding, replace=replace), document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_plot(series: list[dict], document_id: str, revision: int, page_name: str = "figure1",
               graph_path: str | None = None, x_label: str | None = None, y_label: str | None = None,
               x_log: bool | None = None, y_log: bool | None = None, request_id: str | None = None) -> dict[str, Any]:
    """Create native line/scatter/errorbar series. Each series: x,y dataset names; optional name,label,
    kind,color,width,error (symmetric-error dataset). Use a new page_name or an existing graph_path.
    All changes form one undoable operation. Log coordinates must be positive.
    A plotting Skill is optional: translate its applicable design guidance into native Veusz settings;
    explicit user requirements take priority. Without a Skill, plot normally.
    """
    params = dict(series=series, page_name=page_name)
    for name, value in (("graph_path", graph_path), ("x_label", x_label), ("y_label", y_label),
                        ("x_log", x_log), ("y_log", y_log)):
        if value is not None:
            params[name] = value
    return invoke("plot", params, document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_add_widgets(widgets: list[dict], document_id: str, revision: int,
                      request_id: str | None = None) -> dict[str, Any]:
    """Add ANY registered native Veusz widget, including all 2D/3D chart families and installed plugins.
    Discover legal types, parents and settings with veusz_widget_catalog first.
    Each entry: type,parent (absolute),name,settings,optional autoadd (default true).
    autoadd=false suppresses default children, useful for exact layouts. Batch order allows new parents.
    Nested formatting in settings uses double underscores, e.g. PlotLine__color. Names must be new.
    Use native editable widgets to implement optional Skill layout/annotation guidance.
    Report unsupported widget types rather than embedding a finished figure.
    """
    return invoke("add_widgets", {"widgets": widgets}, document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_apply_changes(changes: list[dict], document_id: str, revision: int,
                         request_id: str | None = None) -> dict[str, Any]:
    """Batch absolute setting changes, each {path,value}. Read valid paths with veusz_inspect first.
    Preserves settings omitted from the patch. Validated before mutation, with one native undo step.
    Translate optional Skill styling guidance into this patch, prioritizing user requirements
    and preserving manual changes outside the requested scope. No Skill is required.
    """
    return invoke("set", {"changes": changes}, document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_undo(document_id: str, revision: int, request_id: str | None = None) -> dict[str, Any]:
    """Undo only the latest unchanged bridge operation. Refuse to undo intervening human edits."""
    return invoke("undo", {}, document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_save(path: str, document_id: str, revision: int, overwrite: bool = False,
               request_id: str | None = None) -> dict[str, Any]:
    """Atomically save editable .vsz under the project, setting the native save filename and dirty flag.
    Existing files require overwrite=true. Also writes a data-source manifest alongside the document.
    """
    return invoke("save", dict(path=path, overwrite=overwrite), document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_checkpoint(document_id: str, revision: int, path: str | None = None,
                     request_id: str | None = None) -> dict[str, Any]:
    """Save a recovery .vsz without changing the working filename, dirty flag, revision or undo history."""
    return invoke("checkpoint", {"path": path} if path else {}, document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_open(path: str, document_id: str, revision: int, request_id: str | None = None) -> dict[str, Any]:
    """Open an existing .vsz in the project through Veusz's trust checks. Refuse unsaved changes.
    Read the new document_id/revision afterwards. Never executes arbitrary supplied Python.
    """
    return invoke("open", {"path": path}, document_id, revision, request_id)


@mcp.tool(annotations=WRITE)
def veusz_export(path: str, document_id: str, revision: int, page: int | None = None,
                 dpi: int = 300, background: str = "#ffffffff", request_id: str | None = None) -> dict[str, Any]:
    """Export current page (or explicit zero-based page) as PNG, SVG or PDF. dpi=50..1200.
    PNG defaults to opaque white. background uses #RRGGBBAA; #ffffff00 is transparent.
    """
    params = dict(path=path, dpi=dpi, background=background)
    if page is not None:
        params["page"] = page
    return invoke("export", params, document_id, revision, request_id)


@mcp.tool(annotations=WRITE, structured_output=False)
def veusz_preview(document_id: str, revision: int) -> list:
    """Return a rendered PNG as an MCP image, tagged with the document revision. Does not change settings."""
    from mcp.types import TextContent
    result = invoke("export", {"path": "runtime/previews/%s.png" % uuid.uuid4().hex, "dpi": 110}, document_id, revision)
    return [TextContent(type="text", text="document_id=%s revision=%s" % (document_id, revision)),
            Image(path=result["path"]).to_image_content()]


@mcp.tool(annotations=READ)
def veusz_operation(request_id: str) -> dict[str, Any]:
    """Query a previous write's queued/running/done/expired state and cached response. Never re-executes it."""
    return invoke("operation", {"request_id": request_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")
