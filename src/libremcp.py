"""
LibreOffice Model Context Protocol Server

Works on documents that are open in the user's running LibreOffice (Writer, Calc, Impress/Draw,
Math, Base) through a local UNO named pipe, plus two file-based tools for documents that are not
open. UNO operations run in ``uno_bridge.py`` under LibreOffice's bundled Python, because the
server's own interpreter cannot import pyuno.

Environment variables:
    LIBREMCP_APPS             Comma list of app tool groups to expose (default: all of
                              writer,calc,impress,math,base). Shared tools are always exposed.
    LIBREMCP_ENABLE_RUN_UNO   Set to 0 to hide the run_uno catch-all tool.
    LIBREMCP_PIPE             UNO pipe name (default: libremcp_<user>).
    LIBREMCP_AUTOSTART        Set to 0 to never launch/attach LibreOffice automatically.
    LIBREMCP_OFFICE_PROGRAM   LibreOffice "program" directory (auto-detected).
    LIBREMCP_OFFICE_PYTHON    Python able to ``import uno`` (auto-detected).
    LIBREMCP_TIMEOUT          Seconds per bridge call (default: 120).
"""

import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import threading

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

VERSION = "2.1.0"
mcp = MCPServer("LibreOffice MCP Server", version=VERSION)

BRIDGE = Path(__file__).with_name("uno_bridge.py")
RESULT_MARKER = "@@LIBREMCP_RESULT@@"
ALL_APPS = ("writer", "calc", "impress", "math", "base")
ENABLED_APPS = {
    app.strip().lower()
    for app in os.environ.get("LIBREMCP_APPS", ",".join(ALL_APPS)).split(",")
    if app.strip()
}
RUN_UNO_ENABLED = os.environ.get("LIBREMCP_ENABLE_RUN_UNO", "1") != "0"
AUTOSTART = os.environ.get("LIBREMCP_AUTOSTART", "1") != "0"
PIPE = os.environ.get("LIBREMCP_PIPE") or "libremcp_" + re.sub(r"\W", "_", getpass.getuser())
TIMEOUT = float(os.environ.get("LIBREMCP_TIMEOUT", "120"))

DOC = "Open document: title, file name, full path, or index from list_open_documents. Omit for the active document."


READ_ONLY_TOOLS = {
    "list_open_documents", "render_page", "read_document_text",
    "writer_get_paragraphs", "writer_find_paragraphs",
    "calc_list_sheets", "calc_get_range", "calc_find",
    "impress_list_slides", "impress_get_slide",
    "math_get_formula",
    "base_list_tables", "base_describe_table", "base_query",
}
# Tools that overwrite files, content, or run arbitrary code (in-document edits stay undoable).
DESTRUCTIVE_TOOLS = {
    "save_document", "convert_document", "run_uno",
    "writer_replace_text", "calc_set_range", "impress_set_shape_text",
    "impress_delete_slide", "math_set_formula",
}
IDEMPOTENT_TOOLS = {"writer_format_paragraphs", "calc_format_range", "calc_set_range",
                    "math_set_formula", "impress_set_shape_text", "save_document"}


def _tool(app: Optional[str] = None):
    """Register a tool (with MCP behaviour annotations) unless its app group is disabled."""
    def decorator(fn):
        if app is not None and app not in ENABLED_APPS:
            return fn
        name = fn.__name__
        read_only = name in READ_ONLY_TOOLS
        annotations = ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=None if read_only else name in DESTRUCTIVE_TOOLS,
            idempotent_hint=True if read_only else name in IDEMPOTENT_TOOLS,
            open_world_hint=False,
        )
        return mcp.tool(title=name.replace("_", " ").capitalize(), annotations=annotations)(fn)
    return decorator


# ---------------------------------------------------------------- LibreOffice discovery

def _office_program_dir() -> Optional[Path]:
    candidates = []
    if os.environ.get("LIBREMCP_OFFICE_PROGRAM"):
        candidates.append(Path(os.environ["LIBREMCP_OFFICE_PROGRAM"]))
    for exe in ("soffice", "libreoffice"):
        found = shutil.which(exe)
        if found:
            candidates.append(Path(found).resolve().parent)
    candidates += [
        Path(r"C:\Program Files\LibreOffice\program"),
        Path(r"C:\Program Files (x86)\LibreOffice\program"),
        Path("/Applications/LibreOffice.app/Contents/MacOS"),
        Path("/usr/lib/libreoffice/program"),
        Path("/opt/libreoffice/program"),
    ]
    for candidate in candidates:
        if any((candidate / name).exists() for name in ("soffice.exe", "soffice", "soffice.bin")):
            return candidate
    return None


def _office_python() -> str:
    if os.environ.get("LIBREMCP_OFFICE_PYTHON"):
        return os.environ["LIBREMCP_OFFICE_PYTHON"]
    program = _office_program_dir()
    if program:
        for candidate in (program / "python.exe", program.parent / "Resources" / "python", program / "python"):
            if candidate.exists():
                return str(candidate)
    # Linux distributions ship pyuno for the system interpreter (e.g. python3-uno).
    return shutil.which("python3") or "python3"


def _soffice_gui() -> str:
    program = _office_program_dir()
    if program:
        for name in ("soffice.exe", "soffice"):
            if (program / name).exists():
                return str(program / name)
    return shutil.which("soffice") or shutil.which("libreoffice") or "soffice"


def _start_office() -> None:
    """Ask LibreOffice to accept UNO connections on the local pipe.

    If LibreOffice is already running, the request is forwarded to that instance (the user's open
    documents stay as they are); otherwise a normal GUI instance is started. Only a named pipe is
    opened, never a TCP port.
    """
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([_soffice_gui(), f"--accept=pipe,name={PIPE};urp;", "--norestore"], **kwargs)


def _invoke_bridge(request: Dict[str, Any]) -> Dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            [_office_python(), str(BRIDGE)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            env=env,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "LibreOffice's Python was not found. Set LIBREMCP_OFFICE_PYTHON to an interpreter "
            "that can 'import uno'."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreOffice did not respond within {TIMEOUT:.0f} s.") from exc
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    raise RuntimeError(
        f"UNO bridge failed (exit {completed.returncode}): {(completed.stderr or completed.stdout)[-2000:]}"
    )


# SDK v2 runs sync tools on worker threads; serialize so concurrent calls cannot both autostart
# LibreOffice or interleave edits on the same document.
_BRIDGE_LOCK = threading.Lock()


def _bridge(op: str, **args: Any) -> Dict[str, Any]:
    request = {"op": op, "args": {k: v for k, v in args.items() if v is not None},
               "pipe": PIPE, "connect_timeout": 3}
    with _BRIDGE_LOCK:
        response = _invoke_bridge(request)
        if not response.get("ok") and response.get("code") == "NO_CONNECTION" and AUTOSTART:
            _start_office()
            request["connect_timeout"] = 60
            response = _invoke_bridge(request)
    if not response.get("ok"):
        message = response.get("error", "Unknown error")
        if response.get("code") == "NO_CONNECTION":
            message += (f" Start LibreOffice with: soffice --accept=\"pipe,name={PIPE};urp;\" "
                        "(or unset LIBREMCP_AUTOSTART=0).")
        if response.get("traceback"):
            message += "\n" + response["traceback"]
        raise RuntimeError(message)
    result = response.get("result")
    if response.get("stdout"):
        result = dict(result or {})
        result["stdout"] = response["stdout"]
    return result


# ---------------------------------------------------------------- shared tools (open documents)

@_tool()
def list_open_documents() -> Dict[str, Any]:
    """List documents open in the running LibreOffice (title, path, type, modified, active)."""
    return _bridge("list_open_documents")


@_tool()
def open_document(path: Optional[str] = None, new_type: Optional[str] = None,
                  readonly: bool = False, hidden: bool = False) -> Dict[str, Any]:
    """Open a file in the running LibreOffice, or create a new document.

    Args:
        path: File to open (returns the existing window if it is already open).
        new_type: Create a new untitled document instead: writer, calc, impress, draw or math.
        readonly: Open read-only.
        hidden: Open without a visible window.
    """
    return _bridge("open_document", path=path, new_type=new_type, readonly=readonly, hidden=hidden)


@_tool()
def save_document(doc: Optional[str] = None, path: Optional[str] = None, copy: bool = False) -> Dict[str, Any]:
    """Save an open document. Without path: save in place. With path: save as (format from the
    extension, e.g. .odt .docx .doc .ods .xlsx .odp .pptx .pdf). copy=True (implied for .pdf)
    writes the file without switching the document to it.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        path: Optional target path.
        copy: Export a copy instead of "save as".
    """
    return _bridge("save_document", doc=doc, path=path, copy=copy)


@_tool()
def render_page(doc: Optional[str] = None, page: int = 1, width: int = 1000,
                save_path: Optional[str] = None) -> Image:
    """Render one page (Writer), slide (Impress/Draw), sheet (Calc) or the formula (Math) of an
    open document to PNG and return the image, to check layout visually in one call.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        page: 1-based page / slide / sheet number.
        width: Image width in pixels.
        save_path: Optionally also write the PNG to this path.
    """
    result = _bridge("render_page", doc=doc, page=page, width=width)
    data = base64.b64decode(result["png_base64"])
    if save_path:
        Path(save_path).write_bytes(data)
    return Image(data=data, format="png")


if RUN_UNO_ENABLED:
    @_tool()
    def run_uno(script: str, doc: Optional[str] = None, undo_title: Optional[str] = None,
                undo_group: bool = True) -> Dict[str, Any]:
        """Catch-all: run Python (pyuno) inside LibreOffice against the running instance. Use it for
        anything the specific tools do not cover, batching many edits into one call.

        Pre-bound names: uno, ctx, smgr, desktop, doc (the resolved document or None), pv(**props)
        -> PropertyValue tuple, filter_data(**props). Assign a JSON-serialisable value to `result`
        to return it; print() output is returned as stdout. Edits are grouped into one undo step.

        Args:
            script: Python source to execute.
            doc: Open document: title, file name, full path, or index. Omit for the active document.
            undo_title: Label for the undo step.
            undo_group: Set False for scripts that close documents or must not be grouped.
        """
        return _bridge("run_uno", script=script, doc=doc, undo_title=undo_title, undo_group=undo_group)


# ---------------------------------------------------------------- writer

@_tool("writer")
def writer_get_paragraphs(doc: Optional[str] = None, start: int = 0, count: int = 40,
                          formatting: bool = True, max_chars: int = 400) -> Dict[str, Any]:
    """Read body paragraphs (and tables) of an open Writer document with their index, text,
    paragraph style, list style/level, font runs, indents (mm, flagging direct overrides),
    spacing and alignment. Indices are 0-based and used by the other writer_* tools.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        start: First paragraph index.
        count: Number of paragraphs to return.
        formatting: Include formatting details.
        max_chars: Truncate paragraph text to this length.
    """
    return _bridge("writer_get_paragraphs", doc=doc, start=start, count=count,
                   formatting=formatting, max_chars=max_chars)


@_tool("writer")
def writer_find_paragraphs(pattern: str, doc: Optional[str] = None, regex: bool = False,
                           case_sensitive: bool = False, max_results: int = 50) -> Dict[str, Any]:
    """Find paragraph indices whose text contains a string or matches a Python regex.

    Args:
        pattern: Text or regular expression.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        regex: Treat pattern as a regular expression.
        case_sensitive: Match case.
        max_results: Maximum matches.
    """
    return _bridge("writer_find_paragraphs", pattern=pattern, doc=doc, regex=regex,
                   case_sensitive=case_sensitive, max_results=max_results)


@_tool("writer")
def writer_format_paragraphs(
    indices: Union[List[Union[int, str]], int, str],
    doc: Optional[str] = None,
    copy_from: Optional[int] = None,
    paragraph_style: Optional[str] = None,
    list_style: Optional[str] = None,
    list_level: Optional[int] = None,
    font_name: Optional[str] = None,
    font_size: Optional[float] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    color: Optional[str] = None,
    left_indent_mm: Optional[float] = None,
    first_line_indent_mm: Optional[float] = None,
    space_before_mm: Optional[float] = None,
    space_after_mm: Optional[float] = None,
    align: Optional[str] = None,
    reset_indents: bool = False,
    reset_properties: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Format many paragraphs in one call (one undo step). Best practice: copy_from a paragraph
    that already looks right, then override only what differs.

    Args:
        indices: Paragraph indices, e.g. [12, 13] or ["12-16"]; negatives count from the end.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        copy_from: Copy paragraph style, list style/level, indents, spacing, alignment and the
            first run's font from this paragraph (inherited values stay inherited).
        paragraph_style: Paragraph style name.
        list_style: List (numbering) style name, e.g. "List 1"; "" removes the list.
        list_level: 0-based list level.
        font_name: Font family.
        font_size: Font size in pt.
        bold: Bold on/off.
        italic: Italic on/off.
        underline: Single underline on/off.
        color: Text colour as hex "RRGGBB" or "auto".
        left_indent_mm: Left indent.
        first_line_indent_mm: First-line indent (negative for hanging).
        space_before_mm: Space above.
        space_after_mm: Space below.
        align: left, center, right or justify.
        reset_indents: Remove direct indent overrides so list/paragraph style indents apply.
        reset_properties: Other UNO property names to reset to the style default.
        extra: Raw UNO property name -> value (Para*/Numbering* go to the paragraph, others to the text).
    """
    return _bridge("writer_format_paragraphs", indices=indices, doc=doc, copy_from=copy_from,
                   paragraph_style=paragraph_style, list_style=list_style, list_level=list_level,
                   font_name=font_name, font_size=font_size, bold=bold, italic=italic,
                   underline=underline, color=color, left_indent_mm=left_indent_mm,
                   first_line_indent_mm=first_line_indent_mm, space_before_mm=space_before_mm,
                   space_after_mm=space_after_mm, align=align, reset_indents=reset_indents,
                   reset_properties=reset_properties, extra=extra)


@_tool("writer")
def writer_insert_paragraphs(texts: Union[List[str], str], after_index: Optional[int] = None,
                             doc: Optional[str] = None,
                             copy_format_from: Optional[int] = None) -> Dict[str, Any]:
    """Insert new paragraphs after a paragraph (they inherit its formatting).

    Args:
        texts: Paragraph texts to insert, in order.
        after_index: Insert after this paragraph index; -1 inserts at the start; default: end.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        copy_format_from: Optionally copy formatting from another paragraph index (pre-insert index).
    """
    return _bridge("writer_insert_paragraphs", texts=texts, after_index=after_index, doc=doc,
                   copy_format_from=copy_format_from)


@_tool("writer")
def writer_replace_text(search: str, replace: str, doc: Optional[str] = None, regex: bool = False,
                        case_sensitive: bool = False, whole_words: bool = False) -> Dict[str, Any]:
    """Find and replace text throughout an open Writer document, keeping formatting.

    Args:
        search: Text or LibreOffice (ICU) regular expression.
        replace: Replacement ($1 etc. for regex groups).
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        regex: Use regular expressions.
        case_sensitive: Match case.
        whole_words: Whole words only.
    """
    return _bridge("writer_replace_text", search=search, replace=replace, doc=doc, regex=regex,
                   case_sensitive=case_sensitive, whole_words=whole_words)


# ---------------------------------------------------------------- calc

@_tool("calc")
def calc_list_sheets(doc: Optional[str] = None) -> Dict[str, Any]:
    """List sheets of an open Calc document with used range and active sheet.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("calc_list_sheets", doc=doc)


@_tool("calc")
def calc_get_range(range: Optional[str] = None, sheet: Optional[str] = None, doc: Optional[str] = None,
                   formulas: bool = False, max_cells: int = 2000) -> Dict[str, Any]:
    """Read cell values (or formulas) from an open Calc sheet as a 2-D array.

    Args:
        range: A1-style range such as "A1:D20"; omit for the sheet's used area.
        sheet: Sheet name or 0-based index; omit for the active sheet.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        formulas: Return formulas instead of values.
        max_cells: Refuse ranges larger than this.
    """
    return _bridge("calc_get_range", range=range, sheet=sheet, doc=doc, formulas=formulas,
                   max_cells=max_cells)


@_tool("calc")
def calc_set_range(values: List[List[Any]], start: str = "A1", sheet: Optional[str] = None,
                   doc: Optional[str] = None) -> Dict[str, Any]:
    """Write a 2-D array of values into an open Calc sheet starting at a cell (one undo step).
    Numbers are stored as numbers, strings starting with "=" as formulas, null clears a cell.

    Args:
        values: Rows of cell values.
        start: Top-left cell, e.g. "B2".
        sheet: Sheet name or 0-based index; omit for the active sheet.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("calc_set_range", values=values, start=start, sheet=sheet, doc=doc)


@_tool("calc")
def calc_format_range(range: str, sheet: Optional[str] = None, doc: Optional[str] = None,
                      bold: Optional[bool] = None, italic: Optional[bool] = None,
                      font_name: Optional[str] = None, font_size: Optional[float] = None,
                      color: Optional[str] = None, background: Optional[str] = None,
                      number_format: Optional[str] = None, align: Optional[str] = None,
                      wrap: Optional[bool] = None, column_width_mm: Optional[float] = None,
                      autofit_columns: bool = False,
                      extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Format a cell range in an open Calc sheet (one undo step).

    Args:
        range: A1-style range, e.g. "A1:F1".
        sheet: Sheet name or 0-based index; omit for the active sheet.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        bold: Bold on/off.
        italic: Italic on/off.
        font_name: Font family.
        font_size: Font size in pt.
        color: Text colour hex "RRGGBB".
        background: Cell background hex "RRGGBB" or "auto".
        number_format: Format code, e.g. "#,##0.00", "0%", "YYYY-MM-DD".
        align: left, center, right or justify.
        wrap: Wrap text.
        column_width_mm: Set the width of every column in the range.
        autofit_columns: Set optimal column width.
        extra: Raw UNO cell property name -> value.
    """
    return _bridge("calc_format_range", range=range, sheet=sheet, doc=doc, bold=bold, italic=italic,
                   font_name=font_name, font_size=font_size, color=color, background=background,
                   number_format=number_format, align=align, wrap=wrap,
                   column_width_mm=column_width_mm, autofit_columns=autofit_columns, extra=extra)


@_tool("calc")
def calc_find(text: str, sheet: Optional[str] = None, doc: Optional[str] = None, regex: bool = False,
              case_sensitive: bool = False, max_results: int = 50) -> Dict[str, Any]:
    """Find cells containing text in an open Calc document (all sheets unless sheet is given).

    Args:
        text: Text or regular expression.
        sheet: Limit to this sheet.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
        regex: Use regular expressions.
        case_sensitive: Match case.
        max_results: Maximum matches.
    """
    return _bridge("calc_find", text=text, sheet=sheet, doc=doc, regex=regex,
                   case_sensitive=case_sensitive, max_results=max_results)


# ---------------------------------------------------------------- impress / draw

@_tool("impress")
def impress_list_slides(doc: Optional[str] = None) -> Dict[str, Any]:
    """List slides (1-based) of an open Impress/Draw document with title, layout, shape count, notes.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("impress_list_slides", doc=doc)


@_tool("impress")
def impress_get_slide(slide: int = 1, doc: Optional[str] = None) -> Dict[str, Any]:
    """List the shapes on a slide with index, type, name, position/size (mm) and text.

    Args:
        slide: 1-based slide number.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("impress_get_slide", slide=slide, doc=doc)


@_tool("impress")
def impress_set_shape_text(slide: int, shape_index: int, text: Union[str, List[str]],
                           doc: Optional[str] = None) -> Dict[str, Any]:
    """Replace the text of a shape (a list becomes one paragraph/bullet per item).

    Args:
        slide: 1-based slide number.
        shape_index: Shape index from impress_get_slide.
        text: New text or list of lines.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("impress_set_shape_text", slide=slide, shape_index=shape_index, text=text, doc=doc)


@_tool("impress")
def impress_add_slide(after: Optional[int] = None, title: Optional[str] = None,
                      body: Optional[Union[str, List[str]]] = None, layout: Optional[int] = None,
                      doc: Optional[str] = None) -> Dict[str, Any]:
    """Insert a slide after a given slide and fill its title/body placeholders.

    Args:
        after: 1-based slide to insert after (default: last).
        title: Title text.
        body: Body text or list of bullet lines.
        layout: AutoLayout number (0 title slide, 1 title+content, 19 title only, 20 blank);
            chosen automatically when omitted.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("impress_add_slide", after=after, title=title, body=body, layout=layout, doc=doc)


@_tool("impress")
def impress_delete_slide(slide: int, doc: Optional[str] = None) -> Dict[str, Any]:
    """Delete a slide.

    Args:
        slide: 1-based slide number.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("impress_delete_slide", slide=slide, doc=doc)


# ---------------------------------------------------------------- math

@_tool("math")
def math_get_formula(doc: Optional[str] = None) -> Dict[str, Any]:
    """Read the StarMath formula of an open Math document.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("math_get_formula", doc=doc)


@_tool("math")
def math_set_formula(formula: str, base_font_size: Optional[int] = None,
                     doc: Optional[str] = None) -> Dict[str, Any]:
    """Set the StarMath formula (e.g. "a^2 + b^2 = c^2") of an open Math document.

    Args:
        formula: StarMath source.
        base_font_size: Optional base font size in pt.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("math_set_formula", formula=formula, base_font_size=base_font_size, doc=doc)


# ---------------------------------------------------------------- base

@_tool("base")
def base_list_tables(doc: Optional[str] = None) -> Dict[str, Any]:
    """List tables and saved queries of an open Base (.odb) database.

    Args:
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("base_list_tables", doc=doc)


@_tool("base")
def base_describe_table(table: str, doc: Optional[str] = None) -> Dict[str, Any]:
    """Describe the columns of a table in an open Base database.

    Args:
        table: Table name.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("base_describe_table", table=table, doc=doc)


@_tool("base")
def base_query(sql: str, max_rows: int = 200, doc: Optional[str] = None) -> Dict[str, Any]:
    """Run one read-only SELECT/WITH/VALUES statement against an open Base database.
    Use run_uno for data changes.

    Args:
        sql: The query.
        max_rows: Maximum rows to return.
        doc: Open document: title, file name, full path, or index. Omit for the active document.
    """
    return _bridge("base_query", sql=sql, max_rows=max_rows, doc=doc)


# ---------------------------------------------------------------- file-based tools (closed files)

class TextContent(BaseModel):
    """Text content extracted from a document"""
    content: str = Field(description="The extracted text content")
    word_count: int = Field(description="Number of words in the content")
    char_count: int = Field(description="Number of characters in the content")


class ConversionResult(BaseModel):
    """Result of document conversion"""
    source_path: str = Field(description="Source document path")
    target_path: str = Field(description="Target document path")
    target_format: str = Field(description="Converted format")
    success: bool = Field(description="Whether conversion was successful")
    error_message: Optional[str] = Field(default=None, description="Error message if conversion failed")


def _run_libreoffice_command(args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a headless LibreOffice command in an isolated, temporary profile."""
    executables = []
    program = _office_program_dir()
    if program and os.name == "nt" and (program / "soffice.com").exists():
        # The Windows console launcher waits for headless operations to finish.
        executables.append(str(program / "soffice.com"))
    executables += ["soffice.com"] if os.name == "nt" else []
    executables += [_soffice_gui(), "libreoffice", "soffice"]
    try:
        for executable in executables:
            try:
                with tempfile.TemporaryDirectory(prefix="libremcp_profile_") as profile:
                    cmd = [executable, f"-env:UserInstallation={Path(profile).as_uri()}", *args]
                    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                          text=True, timeout=timeout, check=False)
            except FileNotFoundError:
                continue
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"LibreOffice command timed out after {timeout} seconds") from exc
    raise FileNotFoundError("LibreOffice executable not found. Please install LibreOffice.")


def _extract_text_from_odt(file_path: str) -> str:
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(file_path, "r") as archive:
        root = ET.fromstring(archive.read("content.xml").decode("utf-8"))
    parts = []
    for elem in root.iter():
        if elem.text:
            parts.append(elem.text)
        if elem.tail:
            parts.append(elem.tail)
    return " ".join(parts).strip()


@_tool()
def read_document_text(path: str) -> TextContent:
    """Extract plain text from a document file on disk (does not need it to be open).

    Args:
        path: Path to the document file.
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Document not found: {path}")
    with tempfile.TemporaryDirectory() as tmp_dir:
        result = _run_libreoffice_command(
            ["--headless", "--convert-to", "txt:Text (encoded):UTF8", "--outdir", tmp_dir, str(path_obj)])
        txt_files = list(Path(tmp_dir).glob("*.txt"))
        if txt_files:
            content = txt_files[0].read_text(encoding="utf-8", errors="ignore")
        elif path_obj.suffix.lower() == ".odt":
            content = _extract_text_from_odt(str(path_obj))
        else:
            raise RuntimeError(f"Could not extract text. LibreOffice output: {result.stderr}")
    return TextContent(content=content, word_count=len(content.split()), char_count=len(content))


@_tool()
def convert_document(source_path: str, target_path: str, target_format: str) -> ConversionResult:
    """Convert a document file on disk to another format with headless LibreOffice.

    Args:
        source_path: Source document.
        target_path: Output file path.
        target_format: Target format (pdf, docx, odt, xlsx, ods, pptx, html, txt, csv, ...).
    """
    source = Path(source_path)
    target = Path(target_path)
    if not source.exists():
        return ConversionResult(source_path=source_path, target_path=target_path,
                                target_format=target_format, success=False,
                                error_message=f"Source file not found: {source_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _run_libreoffice_command(
                ["--headless", "--convert-to", target_format, "--outdir", tmp_dir, str(source)])
            produced = Path(tmp_dir) / f"{source.stem}.{target_format.split(':')[0]}"
            if produced.exists():
                shutil.move(str(produced), str(target))
        success = target.exists()
        return ConversionResult(source_path=source_path, target_path=str(target),
                                target_format=target_format, success=success,
                                error_message=None if success else f"Conversion failed: {result.stderr}")
    except Exception as exc:
        return ConversionResult(source_path=source_path, target_path=target_path,
                                target_format=target_format, success=False, error_message=str(exc))


# ---------------------------------------------------------------- entry point

def main():
    """Run the LibreOffice MCP server (stdio)."""
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("--help", "-h"):
            print(__doc__)
            print("Usage: python src/main.py [--check | --version | --help]")
            return
        if arg == "--version":
            print(f"LibreOffice MCP Server v{VERSION}")
            return
        if arg == "--check":
            print(f"program dir : {_office_program_dir()}")
            print(f"office python: {_office_python()}")
            print(f"pipe        : {PIPE}")
            print(f"apps        : {sorted(ENABLED_APPS)}  run_uno: {RUN_UNO_ENABLED}")
            global AUTOSTART
            AUTOSTART = False  # a diagnostic must not launch LibreOffice
            try:
                print(json.dumps(_bridge("list_open_documents"), indent=2))
            except Exception as exc:
                print(f"bridge      : {exc}")
            return
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
