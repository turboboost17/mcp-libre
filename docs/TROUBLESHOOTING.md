# Troubleshooting

Start with:

```bash
uv run --locked python src/main.py --check
```

## `NO_CONNECTION`: could not connect to LibreOffice

**Cause.** LibreOffice isn't accepting UNO connections on the pipe.

**Fixes:**
- With `LIBREMCP_AUTOSTART` enabled (the default), the server asks LibreOffice to accept
  connections. Allow up to about 60 s on the first call while LibreOffice starts.
- Otherwise, start it yourself, using the pipe name printed by `--check`:
  ```bash
  soffice --accept="pipe,name=libremcp_<user>;urp;"
  ```
- Several MCP clients or users on one machine: give each one its own `LIBREMCP_PIPE`.

## "LibreOffice's Python was not found" or `import uno` fails

**Fixes:**
- Set `LIBREMCP_OFFICE_PYTHON` to an interpreter that can import `uno`:
  - **Windows:** `...\LibreOffice\program\python.exe`
  - **Linux:** `/usr/bin/python3` with `python3-uno` installed.
- Set `LIBREMCP_OFFICE_PROGRAM` if LibreOffice isn't installed in the usual location.
- The server already removes `PYTHONHOME`, `PYTHONPATH` and `VIRTUAL_ENV` before it starts
  LibreOffice's Python. If imports still fail, check for a global `sitecustomize` or a
  `PYTHONSTARTUP` file.

## `NO_DOCUMENT`: could not resolve document

**Cause.** The `doc` value doesn't match any open document. The error message lists the titles of
the documents that are open.

**Fixes:**
- Pass one of the listed titles, a file name, a full path, or the index from
  `list_open_documents`.
- To use the active document, leave out `doc`.

## Index out of range in Writer tools

**Cause.** Inserting or deleting paragraphs shifts the indices after them.

**Fix.** Run `writer_find_paragraphs` again to get current indices.

## Call times out

**Fixes:**
- Increase `LIBREMCP_TIMEOUT` (the default is 120 s).
- If a dialog is open in LibreOffice, close it. A modal dialog blocks UNO calls.

## Formatting doesn't "stick" or bullets are misaligned

**Cause.** Direct indent overrides take priority over list and style indents.
`writer_get_paragraphs` shows these overrides as `direct_indent`.

**Fix.** Run `writer_format_paragraphs` with `copy_from=<a good paragraph>` and
`reset_indents=True`.

## File conversion (`convert_document`, `read_document_text`) fails

**Cause.** These tools run a separate headless LibreOffice with a temporary profile, so they work
even when your LibreOffice is open.

**Fixes:**
- Check that the source path exists.
- Check that the target format is supported for that document type, for example `pdf`, `docx`,
  `odt`, `xlsx` or `pptx`.
