"""UNO bridge executed by LibreOffice's bundled Python (it must be able to ``import uno``).

The MCP server (which runs in its own virtual environment and cannot import pyuno) launches
this script once per tool call. It reads a JSON request ``{"op", "args", "pipe",
"connect_timeout"}`` from stdin, connects to the user's running LibreOffice over a local named
pipe (no TCP listener), performs the operation and prints a single JSON response line prefixed
with ``RESULT_MARKER``. Only the standard library and pyuno are used.
"""

import base64
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback

import uno
from com.sun.star.beans import PropertyValue

RESULT_MARKER = "@@LIBREMCP_RESULT@@"

SERVICE_TYPES = (
    ("com.sun.star.text.TextDocument", "writer"),
    ("com.sun.star.sheet.SpreadsheetDocument", "calc"),
    ("com.sun.star.presentation.PresentationDocument", "impress"),
    ("com.sun.star.drawing.DrawingDocument", "draw"),
    ("com.sun.star.formula.FormulaProperties", "math"),
    ("com.sun.star.sdb.OfficeDatabaseDocument", "base"),
)

FACTORIES = {
    "writer": "private:factory/swriter",
    "calc": "private:factory/scalc",
    "impress": "private:factory/simpress",
    "draw": "private:factory/sdraw",
    "math": "private:factory/smath",
}

SAVE_FILTERS = {
    "writer": {"odt": "writer8", "docx": "MS Word 2007 XML", "doc": "MS Word 97",
               "rtf": "Rich Text Format", "txt": "Text", "html": "HTML (StarWriter)",
               "pdf": "writer_pdf_Export"},
    "calc": {"ods": "calc8", "xlsx": "Calc MS Excel 2007 XML", "xls": "MS Excel 97",
             "csv": "Text - txt - csv (StarCalc)", "pdf": "calc_pdf_Export"},
    "impress": {"odp": "impress8", "pptx": "Impress MS PowerPoint 2007 XML",
                "ppt": "MS PowerPoint 97", "pdf": "impress_pdf_Export"},
    "draw": {"odg": "draw8", "pdf": "draw_pdf_Export"},
    "math": {"odf": "math8", "mml": "MathML XML (Math)", "pdf": "math_pdf_Export"},
}

ALIGN = {"left": 0, "right": 1, "justify": 2, "center": 3}
ALIGN_NAMES = {v: k for k, v in ALIGN.items()}
MATH_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"


class BridgeError(Exception):
    def __init__(self, message, code="ERROR"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- helpers

def pv(**kwargs):
    props = []
    for name, value in kwargs.items():
        prop = PropertyValue()
        prop.Name = name
        prop.Value = value
        props.append(prop)
    return tuple(props)


def filter_data(**kwargs):
    return uno.Any("[]com.sun.star.beans.PropertyValue", pv(**kwargs))


def mm(value):
    return round(value / 100.0, 2)


def hundredth_mm(value):
    return int(round(float(value) * 100))


def to_json(value, depth=0):
    if depth > 6:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [to_json(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): to_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, uno.Enum):
        return value.value
    return repr(value)


def connect(pipe, timeout):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)
    url = "uno:pipe,name=%s;urp;StarOffice.ComponentContext" % pipe
    deadline = time.time() + timeout
    while True:
        try:
            ctx = resolver.resolve(url)
            break
        except Exception as exc:  # NoConnectException is not importable as a class reliably
            if time.time() >= deadline:
                raise BridgeError("LibreOffice is not accepting connections on pipe '%s': %s"
                                  % (pipe, getattr(exc, "Message", exc)), "NO_CONNECTION")
            time.sleep(0.3)
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    return ctx, desktop


def doc_type(doc):
    for service, name in SERVICE_TYPES:
        try:
            if doc.supportsService(service):
                return name
        except Exception:
            pass
    return None


def office_documents(desktop):
    docs = []
    enum = desktop.Components.createEnumeration()
    while enum.hasMoreElements():
        comp = enum.nextElement()
        if doc_type(comp):
            docs.append(comp)
    return docs


def doc_path(doc):
    url = doc.URL if hasattr(doc, "URL") else ""
    if url.startswith("file:"):
        return uno.fileUrlToSystemPath(url)
    return None


def doc_title(doc):
    try:
        title = doc.CurrentController.Frame.Title
    except Exception:
        try:
            title = doc.Title
        except Exception:
            return ""
    return re.sub(r"\s+[\u2014\u2013-]\s+LibreOffice\b.*$", "", title or "")


def describe(doc, index=None):
    info = {
        "title": doc_title(doc),
        "path": doc_path(doc),
        "url": getattr(doc, "URL", ""),
        "type": doc_type(doc),
    }
    try:
        info["modified"] = bool(doc.isModified())
    except Exception:
        pass
    try:
        info["readonly"] = bool(doc.isReadonly())
    except Exception:
        pass
    if index is not None:
        info["index"] = index
    return info


def norm(path):
    return os.path.normcase(os.path.abspath(path))


def find_doc(desktop, ref, expected=None):
    docs = office_documents(desktop)
    if not docs:
        raise BridgeError("No documents are open in LibreOffice.", "NO_DOCUMENT")
    target = None
    if ref in (None, ""):
        current = desktop.CurrentComponent
        if current is not None and doc_type(current):
            target = current
        elif len(docs) == 1:
            target = docs[0]
        else:
            candidates = [d for d in docs if expected is None or doc_type(d) == expected]
            if len(candidates) == 1:
                target = candidates[0]
    else:
        ref = str(ref)
        ref_path = None
        if ref.startswith("file:"):
            ref_path = norm(uno.fileUrlToSystemPath(ref))
        elif os.path.isabs(ref):
            ref_path = norm(ref)
        for d in docs:
            path = doc_path(d)
            if ref_path and path and norm(path) == ref_path:
                target = d
                break
            names = {doc_title(d)}
            if path:
                names.add(os.path.basename(path))
            if ref in names or ref.lower() in {n.lower() for n in names if n}:
                target = d
                break
        if target is None and ref.isdigit() and int(ref) < len(docs):
            target = docs[int(ref)]
    if target is None:
        listing = ", ".join("%r" % doc_title(d) for d in docs)
        raise BridgeError("Could not resolve document %r. Open documents: %s" % (ref, listing),
                          "NO_DOCUMENT")
    kind = doc_type(target)
    if expected and kind != expected and not (expected == "impress" and kind == "draw"):
        raise BridgeError("Document %r is a %s document, not %s." % (doc_title(target), kind, expected))
    return target


@contextlib.contextmanager
def undo_context(doc, title):
    manager = getattr(doc, "UndoManager", None)
    if manager is not None:
        manager.enterUndoContext(title)
    try:
        yield
    finally:
        if manager is not None:
            try:
                manager.leaveUndoContext()
            except Exception:
                pass  # the script may have closed the document


def parse_indices(spec, total):
    """Accept an int, a list of ints, or strings like "3-7"; negative ints count from the end."""
    if spec is None:
        return []
    if isinstance(spec, (int, str)):
        spec = [spec]
    out = []
    for item in spec:
        if isinstance(item, str) and "-" in item.strip()[1:]:
            a, b = item.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            i = int(item)
            out.append(i + total if i < 0 else i)
    for i in out:
        if i < 0 or i >= total:
            raise BridgeError("Index %d is out of range (0..%d)." % (i, total - 1))
    return sorted(set(out))


def color_int(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lstrip("#")
    if text.lower() == "auto":
        return -1
    return int(text, 16)


# ---------------------------------------------------------------- shared ops

def op_list_open_documents(ctx, desktop, args):
    current = desktop.CurrentComponent
    docs = []
    for i, d in enumerate(office_documents(desktop)):
        info = describe(d, i)
        info["active"] = current is not None and d == current
        docs.append(info)
    return {"documents": docs}


def op_open_document(ctx, desktop, args):
    path = args.get("path")
    new_type = args.get("new_type")
    hidden = bool(args.get("hidden", False))
    if path:
        path = os.path.abspath(path)
        for d in office_documents(desktop):
            if doc_path(d) and norm(doc_path(d)) == norm(path):
                return {"already_open": True, **describe(d)}
        if not os.path.exists(path):
            raise BridgeError("File not found: %s" % path)
        url = uno.systemPathToFileUrl(path)
    elif new_type:
        if new_type not in FACTORIES:
            raise BridgeError("new_type must be one of %s" % sorted(FACTORIES))
        url = FACTORIES[new_type]
    else:
        raise BridgeError("Provide either path or new_type.")
    doc = desktop.loadComponentFromURL(
        url, "_default", 0, pv(Hidden=hidden, ReadOnly=bool(args.get("readonly", False))))
    if doc is None:
        raise BridgeError("LibreOffice could not open %s" % url)
    return {"already_open": False, **describe(doc)}


def op_save_document(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"))
    kind = doc_type(doc)
    path = args.get("path")
    if not path:
        if not doc.hasLocation():
            raise BridgeError("Document has never been saved; provide a path.")
        if doc.isReadonly():
            raise BridgeError("Document is read-only.")
        doc.store()
        return {"saved": True, "path": doc_path(doc)}
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    filters = SAVE_FILTERS.get(kind, {})
    if ext not in filters:
        raise BridgeError("Cannot save a %s document as .%s (supported: %s)"
                          % (kind, ext, ", ".join(sorted(filters)) or "none"))
    url = uno.systemPathToFileUrl(path)
    as_copy = bool(args.get("copy", False)) or ext == "pdf"
    props = pv(FilterName=filters[ext], Overwrite=True)
    if as_copy:
        doc.storeToURL(url, props)
    else:
        doc.storeAsURL(url, props)
    return {"saved": True, "path": path, "filter": filters[ext], "copy": as_copy}


def _export_shape_png(ctx, shape_or_page, path, width):
    exporter = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.drawing.GraphicExportFilter", ctx)
    exporter.setSourceDocument(shape_or_page)
    data = {"PixelWidth": width}
    try:
        size = shape_or_page.Size
        w, h = size.Width, size.Height
    except Exception:
        w, h = shape_or_page.Width, shape_or_page.Height
    if w:
        data["PixelHeight"] = max(1, int(width * h / w))
    exporter.filter(pv(URL=uno.systemPathToFileUrl(path), MediaType="image/png",
                       FilterData=filter_data(**data)))


def op_render_page(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"))
    kind = doc_type(doc)
    page = int(args.get("page") or 1)
    width = int(args.get("width") or 1000)
    fd, path = tempfile.mkstemp(prefix="libremcp_", suffix=".png")
    os.close(fd)
    try:
        if kind == "writer":
            controller = doc.CurrentController
            view_cursor = controller.ViewCursor
            saved = view_cursor.getStart()
            try:
                view_cursor.jumpToLastPage()
                pages = view_cursor.Page
                if page < 1 or page > pages:
                    raise BridgeError("Page %d out of range (1..%d)." % (page, pages))
                view_cursor.jumpToPage(page)
                style = doc.StyleFamilies.getByName("PageStyles").getByName(view_cursor.PageStyleName)
                height = max(1, int(width * style.Height / style.Width))
                doc.storeToURL(uno.systemPathToFileUrl(path), pv(
                    FilterName="writer_png_Export",
                    FilterData=filter_data(PixelWidth=width, PixelHeight=height)))
            finally:
                view_cursor.gotoRange(saved, False)
            total = pages
        elif kind in ("impress", "draw"):
            pages = doc.DrawPages
            total = pages.Count
            if page < 1 or page > total:
                raise BridgeError("Slide %d out of range (1..%d)." % (page, total))
            _export_shape_png(ctx, pages.getByIndex(page - 1), path, width)
        elif kind == "calc":
            sheets = doc.Sheets
            total = sheets.Count
            if page < 1 or page > total:
                raise BridgeError("Sheet %d out of range (1..%d)." % (page, total))
            controller = doc.CurrentController
            previous = controller.ActiveSheet
            controller.setActiveSheet(sheets.getByIndex(page - 1))
            try:
                doc.storeToURL(uno.systemPathToFileUrl(path), pv(FilterName="calc_png_Export"))
            finally:
                controller.setActiveSheet(previous)
        elif kind == "math":
            total = 1
            draw = desktop.loadComponentFromURL(FACTORIES["draw"], "_blank", 0, pv(Hidden=True))
            try:
                shape = draw.createInstance("com.sun.star.drawing.OLE2Shape")
                draw.DrawPages.getByIndex(0).add(shape)
                shape.CLSID = MATH_CLSID
                shape.Model.Formula = doc.Formula
                shape.Model.BaseFontHeight = doc.BaseFontHeight
                shape.Size = shape.Model.VisualAreaSize if hasattr(shape.Model, "VisualAreaSize") else shape.Size
                _export_shape_png(ctx, shape, path, width)
            finally:
                draw.close(True)
        else:
            raise BridgeError("Rendering is not supported for %s documents." % kind)
        with open(path, "rb") as handle:
            data = handle.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return {"png_base64": base64.b64encode(data).decode("ascii"), "page": page,
            "page_count": total, "type": kind}


def op_run_uno(ctx, desktop, args):
    script = args.get("script") or ""
    doc = None
    ref = args.get("doc")
    try:
        doc = find_doc(desktop, ref)
    except BridgeError:
        if ref not in (None, ""):
            raise
    namespace = {
        "uno": uno, "ctx": ctx, "smgr": ctx.ServiceManager, "desktop": desktop, "doc": doc,
        "pv": pv, "filter_data": filter_data, "result": None,
    }
    code = compile(script, "<run_uno>", "exec")
    if doc is not None and args.get("undo_group", True):
        with undo_context(doc, args.get("undo_title") or "MCP script"):
            exec(code, namespace)
    else:
        exec(code, namespace)
    return {"result": to_json(namespace.get("result"))}


# ---------------------------------------------------------------- writer

PARA_COPY_PROPS = ("ParaStyleName", "NumberingStyleName", "NumberingLevel", "ParaLeftMargin",
                   "ParaRightMargin", "ParaFirstLineIndent", "ParaTopMargin", "ParaBottomMargin",
                   "ParaAdjust", "ParaLineSpacing", "ParaIsAutoFirstLineIndent")
CHAR_COPY_PROPS = ("CharFontName", "CharHeight", "CharWeight", "CharPosture", "CharUnderline",
                   "CharColor")


def writer_elements(doc):
    out = []
    enum = doc.Text.createEnumeration()
    while enum.hasMoreElements():
        out.append(enum.nextElement())
    return out


def is_paragraph(el):
    return el.supportsService("com.sun.star.text.Paragraph")


def text_runs(par):
    runs = []
    enum = par.createEnumeration()
    while enum.hasMoreElements():
        portion = enum.nextElement()
        if getattr(portion, "TextPortionType", "Text") != "Text" or not portion.getString():
            continue
        runs.append(portion)
    return runs


def run_format(portion):
    return {
        "font": portion.CharFontName,
        "size": round(portion.CharHeight, 1),
        "bold": portion.CharWeight > 100,
        "italic": portion.CharPosture.value != "NONE",
    }


def paragraph_info(index, el, formatting, max_chars):
    if not is_paragraph(el):
        info = {"index": index, "kind": "table" if el.supportsService("com.sun.star.text.TextTable") else "other"}
        if info["kind"] == "table":
            info["name"] = el.Name
            info["rows"] = el.Rows.Count
            info["columns"] = el.Columns.Count
            info["text"] = " | ".join(el.getCellByName(n).getString() for n in el.CellNames[:12])[:max_chars]
        return info
    text = el.getString()
    info = {"index": index, "text": text if len(text) <= max_chars else text[:max_chars] + "..."}
    if not formatting:
        return info
    info["style"] = el.ParaStyleName
    if el.NumberingStyleName:
        info["list"] = {"style": el.NumberingStyleName, "level": el.NumberingLevel,
                        "label": getattr(el, "ListLabelString", "")}
    runs = text_runs(el)
    formats = []
    for portion in runs:
        f = run_format(portion)
        if f not in formats:
            formats.append(f)
    if len(formats) == 1:
        info["font"] = formats[0]
    elif formats:
        info["fonts"] = formats[:4]
    info["indent_mm"] = {"left": mm(el.ParaLeftMargin), "first_line": mm(el.ParaFirstLineIndent)}
    direct = [p for p in ("ParaLeftMargin", "ParaFirstLineIndent")
              if el.getPropertyState(p).value == "DIRECT_VALUE"]
    if direct:
        info["direct_indent"] = direct
    info["spacing_mm"] = {"before": mm(el.ParaTopMargin), "after": mm(el.ParaBottomMargin)}
    info["align"] = ALIGN_NAMES.get(el.ParaAdjust, el.ParaAdjust)
    return info


def op_writer_get_paragraphs(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "writer")
    elements = writer_elements(doc)
    start = int(args.get("start") or 0)
    count = int(args.get("count") or 40)
    formatting = bool(args.get("formatting", True))
    max_chars = int(args.get("max_chars") or 400)
    items = [paragraph_info(i, elements[i], formatting, max_chars)
             for i in range(start, min(len(elements), start + count))]
    return {"total": len(elements), "paragraphs": items}


def op_writer_find_paragraphs(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "writer")
    pattern = args.get("pattern") or ""
    flags = 0 if args.get("case_sensitive") else re.IGNORECASE
    regex = re.compile(pattern if args.get("regex") else re.escape(pattern), flags)
    limit = int(args.get("max_results") or 50)
    hits = []
    for i, el in enumerate(writer_elements(doc)):
        if is_paragraph(el):
            text = el.getString()
            if regex.search(text):
                hits.append({"index": i, "text": text[:200]})
                if len(hits) >= limit:
                    break
    return {"matches": hits}


def _is_direct(obj, name):
    try:
        return obj.getPropertyState(name).value == "DIRECT_VALUE"
    except Exception:
        return False


def copy_state(source, target, names):
    for name in names:
        if _is_direct(source, name):
            target.setPropertyValue(name, source.getPropertyValue(name))
        else:
            try:
                target.setPropertyToDefault(name)
            except Exception:
                pass


def copy_char_state(source_para, source_run, target_para, target_cursor):
    """Character attributes may live on the paragraph or on its text portions; check both."""
    for name in CHAR_COPY_PROPS:
        if _is_direct(source_run, name) or _is_direct(source_para, name):
            target_cursor.setPropertyValue(name, source_run.getPropertyValue(name))
        else:
            for obj in (target_cursor, target_para):
                try:
                    obj.setPropertyToDefault(name)
                except Exception:
                    pass


def op_writer_format_paragraphs(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "writer")
    elements = writer_elements(doc)
    targets = parse_indices(args.get("indices"), len(elements))
    if not targets:
        raise BridgeError("indices is required.")
    source = None
    if args.get("copy_from") is not None:
        source_index = parse_indices(args["copy_from"], len(elements))[0]
        source = elements[source_index]
        if not is_paragraph(source):
            raise BridgeError("copy_from must reference a paragraph.")
    para = {}
    if args.get("paragraph_style"):
        para["ParaStyleName"] = args["paragraph_style"]
    if args.get("list_style") is not None:
        para["NumberingStyleName"] = args["list_style"]
    if args.get("list_level") is not None:
        para["NumberingLevel"] = int(args["list_level"])
    for key, prop in (("left_indent_mm", "ParaLeftMargin"), ("first_line_indent_mm", "ParaFirstLineIndent"),
                      ("space_before_mm", "ParaTopMargin"), ("space_after_mm", "ParaBottomMargin")):
        if args.get(key) is not None:
            para[prop] = hundredth_mm(args[key])
    if args.get("align"):
        para["ParaAdjust"] = ALIGN[args["align"]]
    char = {}
    if args.get("font_name"):
        char["CharFontName"] = args["font_name"]
    if args.get("font_size") is not None:
        char["CharHeight"] = float(args["font_size"])
    if args.get("bold") is not None:
        char["CharWeight"] = 150.0 if args["bold"] else 100.0
    if args.get("italic") is not None:
        char["CharPosture"] = uno.Enum("com.sun.star.awt.FontSlant", "ITALIC" if args["italic"] else "NONE")
    if args.get("underline") is not None:
        char["CharUnderline"] = 1 if args["underline"] else 0
    if args.get("color") is not None:
        char["CharColor"] = color_int(args["color"])
    reset = list(args.get("reset_properties") or [])
    if args.get("reset_indents"):
        reset += ["ParaLeftMargin", "ParaFirstLineIndent", "ParaRightMargin"]
    extra = args.get("extra") or {}

    source_runs = text_runs(source) if source is not None else []
    with undo_context(doc, "MCP format paragraphs"):
        for i in targets:
            el = elements[i]
            if not is_paragraph(el):
                continue
            cursor = doc.Text.createTextCursorByRange(el)
            if source is not None:
                copy_state(source, el, PARA_COPY_PROPS)
                if source_runs:
                    copy_char_state(source, source_runs[0], el, cursor)
            for name, value in para.items():
                el.setPropertyValue(name, value)
            for name in reset:
                el.setPropertyToDefault(name)
            for name, value in char.items():
                cursor.setPropertyValue(name, value)
            for name, value in extra.items():
                target = el if name.startswith(("Para", "Numbering")) else cursor
                target.setPropertyValue(name, value)
    return {"formatted": targets,
            "result": [paragraph_info(i, elements[i], True, 120) for i in targets[:5]]}


def op_writer_insert_paragraphs(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "writer")
    elements = writer_elements(doc)
    texts = args.get("texts") or []
    if isinstance(texts, str):
        texts = [texts]
    after = int(args.get("after_index", len(elements) - 1))
    if after < -1 or after >= len(elements):
        raise BridgeError("after_index out of range (-1..%d)." % (len(elements) - 1))
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    text = doc.Text
    with undo_context(doc, "MCP insert paragraphs"):
        if after == -1:
            cursor = text.createTextCursorByRange(elements[0].getStart())
            for t in texts:
                text.insertString(cursor, t, False)
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            first = 0
        else:
            anchor = elements[after]
            if not is_paragraph(anchor):
                raise BridgeError("after_index must reference a paragraph.")
            cursor = text.createTextCursorByRange(anchor.getEnd())
            for t in texts:
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                text.insertString(cursor, t, False)
            first = after + 1
    new_indices = list(range(first, first + len(texts)))
    if args.get("copy_format_from") is not None and new_indices:
        source = int(args["copy_format_from"])
        if source >= first:
            source += len(texts)
        op_writer_format_paragraphs(ctx, desktop, {"doc": args.get("doc"), "indices": new_indices,
                                                   "copy_from": source})
    elements = writer_elements(doc)
    return {"inserted": new_indices,
            "result": [paragraph_info(i, elements[i], True, 120) for i in new_indices[:5]]}


def op_writer_replace_text(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "writer")
    descriptor = doc.createReplaceDescriptor()
    descriptor.SearchString = args.get("search") or ""
    descriptor.ReplaceString = args.get("replace") or ""
    descriptor.SearchRegularExpression = bool(args.get("regex", False))
    descriptor.SearchCaseSensitive = bool(args.get("case_sensitive", False))
    descriptor.SearchWords = bool(args.get("whole_words", False))
    if not descriptor.SearchString:
        raise BridgeError("search is required.")
    with undo_context(doc, "MCP replace text"):
        count = doc.replaceAll(descriptor)
    return {"replacements": count}


# ---------------------------------------------------------------- calc

def calc_sheet(doc, name):
    sheets = doc.Sheets
    if name in (None, ""):
        return doc.CurrentController.ActiveSheet
    if isinstance(name, int) or str(name).isdigit():
        return sheets.getByIndex(int(name))
    if not sheets.hasByName(name):
        raise BridgeError("No sheet named %r (sheets: %s)" % (name, ", ".join(sheets.ElementNames)))
    return sheets.getByName(name)


def used_range(sheet):
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    return cursor


def range_name(rng):
    return rng.AbsoluteName.split(".", 1)[-1].replace("$", "")


def op_calc_list_sheets(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "calc")
    active = doc.CurrentController.ActiveSheet.Name
    out = []
    for i in range(doc.Sheets.Count):
        sheet = doc.Sheets.getByIndex(i)
        out.append({"index": i, "name": sheet.Name, "used_range": range_name(used_range(sheet)),
                    "visible": sheet.IsVisible, "active": sheet.Name == active})
    return {"sheets": out}


def op_calc_get_range(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "calc")
    sheet = calc_sheet(doc, args.get("sheet"))
    rng = sheet.getCellRangeByName(args["range"]) if args.get("range") else used_range(sheet)
    rows = rng.Rows.Count
    cols = rng.Columns.Count
    limit = int(args.get("max_cells") or 2000)
    if rows * cols > limit:
        raise BridgeError("Range has %d cells (> max_cells=%d); request a smaller range." % (rows * cols, limit))
    data = rng.FormulaArray if args.get("formulas") else rng.DataArray
    return {"sheet": sheet.Name, "range": range_name(rng), "values": to_json(data)}


def op_calc_set_range(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "calc")
    sheet = calc_sheet(doc, args.get("sheet"))
    values = args.get("values") or []
    start = sheet.getCellRangeByName(args.get("start") or "A1").RangeAddress
    with undo_context(doc, "MCP set range"):
        for r, row in enumerate(values):
            for c, value in enumerate(row):
                cell = sheet.getCellByPosition(start.StartColumn + c, start.StartRow + r)
                if value is None:
                    cell.setString("")
                elif isinstance(value, bool):
                    cell.setValue(1 if value else 0)
                elif isinstance(value, (int, float)):
                    cell.setValue(value)
                elif isinstance(value, str) and value.startswith("="):
                    cell.setFormula(value)
                else:
                    cell.setString(str(value))
    width = max((len(r) for r in values), default=0)
    end = sheet.getCellRangeByPosition(start.StartColumn, start.StartRow,
                                       start.StartColumn + max(width, 1) - 1,
                                       start.StartRow + max(len(values), 1) - 1)
    return {"sheet": sheet.Name, "range": range_name(end), "values": to_json(end.DataArray)}


def op_calc_format_range(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "calc")
    sheet = calc_sheet(doc, args.get("sheet"))
    rng = sheet.getCellRangeByName(args["range"])
    props = {}
    if args.get("bold") is not None:
        props["CharWeight"] = 150.0 if args["bold"] else 100.0
    if args.get("italic") is not None:
        props["CharPosture"] = uno.Enum("com.sun.star.awt.FontSlant", "ITALIC" if args["italic"] else "NONE")
    if args.get("font_name"):
        props["CharFontName"] = args["font_name"]
    if args.get("font_size") is not None:
        props["CharHeight"] = float(args["font_size"])
    if args.get("color") is not None:
        props["CharColor"] = color_int(args["color"])
    if args.get("background") is not None:
        props["CellBackColor"] = color_int(args["background"])
    if args.get("wrap") is not None:
        props["IsTextWrapped"] = bool(args["wrap"])
    if args.get("align"):
        props["HoriJustify"] = uno.Enum("com.sun.star.table.CellHoriJustify",
                                        {"justify": "BLOCK"}.get(args["align"], args["align"].upper()))
    if args.get("number_format"):
        formats = doc.NumberFormats
        locale = uno.createUnoStruct("com.sun.star.lang.Locale")
        key = formats.queryKey(args["number_format"], locale, False)
        if key == -1:
            key = formats.addNew(args["number_format"], locale)
        props["NumberFormat"] = key
    props.update(args.get("extra") or {})
    with undo_context(doc, "MCP format range"):
        for name, value in props.items():
            rng.setPropertyValue(name, value)
        if args.get("column_width_mm") is not None:
            for i in range(rng.Columns.Count):
                rng.Columns.getByIndex(i).Width = hundredth_mm(args["column_width_mm"])
        if args.get("autofit_columns"):
            for i in range(rng.Columns.Count):
                rng.Columns.getByIndex(i).OptimalWidth = True
    return {"sheet": sheet.Name, "range": range_name(rng), "applied": sorted(props)}


def op_calc_find(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "calc")
    names = [args["sheet"]] if args.get("sheet") else list(doc.Sheets.ElementNames)
    limit = int(args.get("max_results") or 50)
    hits = []
    for name in names:
        sheet = calc_sheet(doc, name)
        descriptor = sheet.createSearchDescriptor()
        descriptor.SearchString = args.get("text") or ""
        descriptor.SearchRegularExpression = bool(args.get("regex", False))
        descriptor.SearchCaseSensitive = bool(args.get("case_sensitive", False))
        found = sheet.findAll(descriptor)
        if found is None:
            continue
        for i in range(found.Count):
            rng = found.getByIndex(i)
            hits.append({"sheet": sheet.Name, "range": range_name(rng),
                         "value": rng.getCellByPosition(0, 0).getString()})
            if len(hits) >= limit:
                return {"matches": hits}
    return {"matches": hits}


# ---------------------------------------------------------------- impress / draw

def slide_at(doc, number):
    pages = doc.DrawPages
    number = int(number)
    if number < 1 or number > pages.Count:
        raise BridgeError("Slide %d out of range (1..%d)." % (number, pages.Count))
    return pages.getByIndex(number - 1)


def shape_text(shape):
    try:
        return shape.getString()
    except Exception:
        return None


def shapes_of(page):
    return [page.getByIndex(i) for i in range(page.Count)]


def short_type(shape):
    return shape.ShapeType.rsplit(".", 1)[-1]


def slide_title(page):
    for shape in shapes_of(page):
        if short_type(shape) == "TitleTextShape":
            return shape_text(shape)
    return None


def op_impress_list_slides(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "impress")
    slides = []
    for i in range(doc.DrawPages.Count):
        page = doc.DrawPages.getByIndex(i)
        info = {"slide": i + 1, "name": page.Name, "title": slide_title(page), "shapes": page.Count}
        try:
            info["layout"] = page.Layout
            notes = [shape_text(s) for s in shapes_of(page.NotesPage) if short_type(s) == "NotesShape"]
            if notes and notes[0]:
                info["notes"] = notes[0][:200]
        except Exception:
            pass
        slides.append(info)
    return {"slides": slides}


def op_impress_get_slide(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "impress")
    page = slide_at(doc, args.get("slide", 1))
    shapes = []
    for i, shape in enumerate(shapes_of(page)):
        info = {"index": i, "type": short_type(shape), "name": shape.Name,
                "x_mm": mm(shape.Position.X), "y_mm": mm(shape.Position.Y),
                "width_mm": mm(shape.Size.Width), "height_mm": mm(shape.Size.Height)}
        text = shape_text(shape)
        if text is not None:
            info["text"] = text
        shapes.append(info)
    return {"slide": int(args.get("slide", 1)), "shapes": shapes}


def op_impress_set_shape_text(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "impress")
    page = slide_at(doc, args.get("slide", 1))
    index = int(args["shape_index"])
    if index < 0 or index >= page.Count:
        raise BridgeError("shape_index out of range (0..%d)." % (page.Count - 1))
    shape = page.getByIndex(index)
    text = args.get("text") or ""
    if isinstance(text, list):
        text = "\n".join(text)
    with undo_context(doc, "MCP set shape text"):
        shape.setString(text)
    return {"slide": int(args.get("slide", 1)), "shape_index": index, "text": shape.getString()}


def op_impress_add_slide(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "impress")
    pages = doc.DrawPages
    after = int(args.get("after") or pages.Count)
    after = max(1, min(after, pages.Count))
    with undo_context(doc, "MCP add slide"):
        page = pages.insertNewByIndex(after - 1)
        position = next(i + 1 for i in range(pages.Count) if pages.getByIndex(i) == page)
        layout = args.get("layout")
        if layout is None:
            layout = 1 if args.get("body") else (19 if args.get("title") else 20)
        page.Layout = int(layout)
        body = args.get("body")
        if isinstance(body, list):
            body = "\n".join(body)
        for shape in shapes_of(page):
            kind = short_type(shape)
            if kind == "TitleTextShape" and args.get("title"):
                shape.setString(args["title"])
            elif kind in ("OutlinerShape", "SubtitleShape") and body:
                shape.setString(body)
    return {"slide": position, "count": pages.Count,
            "shapes": op_impress_get_slide(ctx, desktop, {"doc": args.get("doc"), "slide": position})["shapes"]}


def op_impress_delete_slide(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "impress")
    page = slide_at(doc, args["slide"])
    if doc.DrawPages.Count == 1:
        raise BridgeError("Cannot delete the only slide.")
    with undo_context(doc, "MCP delete slide"):
        doc.DrawPages.remove(page)
    return {"deleted": int(args["slide"]), "count": doc.DrawPages.Count}


# ---------------------------------------------------------------- math

def op_math_get_formula(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "math")
    return {"formula": doc.Formula, "base_font_size": doc.BaseFontHeight}


def op_math_set_formula(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "math")
    with undo_context(doc, "MCP set formula"):
        doc.Formula = args.get("formula") or ""
        if args.get("base_font_size") is not None:
            doc.BaseFontHeight = int(args["base_font_size"])
    return {"formula": doc.Formula, "base_font_size": doc.BaseFontHeight}


# ---------------------------------------------------------------- base

def base_connection(doc):
    source = doc.DataSource
    return source, source.getConnection("", "")


def op_base_list_tables(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "base")
    source, conn = base_connection(doc)
    try:
        return {"tables": list(conn.Tables.ElementNames),
                "queries": list(source.QueryDefinitions.ElementNames)}
    finally:
        conn.close()


def op_base_describe_table(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "base")
    _, conn = base_connection(doc)
    try:
        table = conn.Tables.getByName(args["table"])
        cols = []
        for name in table.Columns.ElementNames:
            col = table.Columns.getByName(name)
            cols.append({"name": name, "type": col.TypeName, "precision": col.Precision,
                         "nullable": col.IsNullable != 0, "auto_increment": col.IsAutoIncrement})
        return {"table": args["table"], "columns": cols}
    finally:
        conn.close()


READ_ONLY_SQL = re.compile(r"^\s*(--[^\n]*\n\s*|/\*.*?\*/\s*)*(select|with|values)\b", re.I | re.S)


def op_base_query(ctx, desktop, args):
    doc = find_doc(desktop, args.get("doc"), "base")
    sql = args.get("sql") or ""
    if not READ_ONLY_SQL.match(sql) or ";" in sql.strip().rstrip(";"):
        raise BridgeError("base_query only runs a single SELECT/WITH/VALUES statement; use run_uno for writes.")
    limit = int(args.get("max_rows") or 200)
    _, conn = base_connection(doc)
    try:
        result = conn.createStatement().executeQuery(sql.strip().rstrip(";"))
        meta = result.MetaData
        columns = [meta.getColumnLabel(i) for i in range(1, meta.ColumnCount + 1)]
        rows = []
        truncated = False
        while result.next():
            if len(rows) >= limit:
                truncated = True
                break
            rows.append([None if result.wasNull() and not v else v
                         for v in (result.getString(i) for i in range(1, len(columns) + 1))])
        return {"columns": columns, "rows": rows, "truncated": truncated}
    finally:
        conn.close()


# ---------------------------------------------------------------- entry point

def main():
    request = json.loads(sys.stdin.read())
    captured = io.StringIO()
    response = {"ok": False}
    try:
        handler = globals().get("op_" + str(request.get("op")))
        if handler is None:
            raise BridgeError("Unknown operation %r" % request.get("op"))
        with contextlib.redirect_stdout(captured):
            ctx, desktop = connect(request.get("pipe") or "libremcp",
                                   float(request.get("connect_timeout") or 5))
            response = {"ok": True, "result": handler(ctx, desktop, request.get("args") or {})}
    except BridgeError as exc:
        response = {"ok": False, "code": exc.code, "error": str(exc)}
    except Exception as exc:
        message = getattr(exc, "Message", "") or str(exc)
        response = {"ok": False, "code": "ERROR", "error": "%s: %s" % (type(exc).__name__, message),
                    "traceback": traceback.format_exc(limit=6)[-3000:]}
    output = captured.getvalue()
    if output:
        response["stdout"] = output[-20000:]
    sys.stdout.write(RESULT_MARKER + json.dumps(response, default=repr) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
