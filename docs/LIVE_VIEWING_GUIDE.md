# Live Editing Guide

## How it works

1. **Connection.** The MCP server connects to your running LibreOffice through UNO over a local
   named pipe (`LIBREMCP_PIPE`, default `libremcp_<user>`). Nothing listens on the network.
2. **First call.**
   - If LibreOffice isn't accepting connections yet, the server runs
     `soffice --accept=pipe,name=<pipe>;urp; --norestore`.
   - If LibreOffice is already open, the request goes to that instance and your documents stay
     open.
   - Set `LIBREMCP_AUTOSTART=0` to turn this off.
3. **Each tool call.** The server runs `src/uno_bridge.py` under LibreOffice's Python. The bridge
   connects, makes the change through the document model, and returns JSON (or a PNG for
   `render_page`).
4. **Effect in your window.** Changes appear immediately in the open window. **Nothing is written
   to disk** until a `save_document` call, or until you save yourself.
5. **Undo.** Each tool call is grouped into one undo step, so Ctrl+Z in LibreOffice reverts the
   whole call.

Documents are **not** closed, converted or reloaded during editing. Their formatting, comments,
tracked changes and embedded objects stay as they are.

## Typical session

```text
list_open_documents                          -> pick the document (title, path or index)
writer_find_paragraphs(pattern="Experience") -> paragraph indices
writer_get_paragraphs(start=40, count=20)    -> styles, lists, fonts, indents (direct overrides flagged)
writer_format_paragraphs(indices=["44-52"], copy_from=30, reset_indents=True)
render_page(page=1)                          -> PNG for a visual check
save_document()                              -> only when you want it saved
```

Calc, Impress and Draw, Math and Base follow the same pattern with their own tools; see
[EXAMPLES.md](EXAMPLES.md).

## Visual checks

`render_page` returns a PNG of the page you ask for:

| Application | What is rendered | Selected by |
|-------------|------------------|-------------|
| Writer | a page | `page` |
| Impress / Draw | a slide | `page` |
| Calc | a sheet | `page` (sheet number) |
| Math | the formula | — |

Add `save_path` to also write the PNG to a file. Rendering doesn't move your cursor, change the
active sheet, or modify the document.

## Limits

- Writer paragraph indices count body paragraphs and tables, in document order. Headers, footers,
  frames and the contents of table cells are not indexed. Use `run_uno` to work on those.
- Tools work on the document model, not the UI. Dialogs and toolbars aren't involved, and macro
  security settings don't apply to `run_uno`.
- Only one LibreOffice process can own a user profile. The server attaches to that process; it
  doesn't start a second one.
