# LibreOffice MCP Server

An MCP server that lets an AI assistant work on the documents **open in your running LibreOffice**
(Writer, Calc, Impress/Draw, Math and Base). Edits show up in your window as they happen, and each
tool call is a single undo step.

It talks to LibreOffice through UNO over a **local named pipe**, so no network port is opened. Each
call runs a short script (`src/uno_bridge.py`) under LibreOffice's own Python, because that is the
interpreter that can `import uno`.

Built on MCP Python SDK v2. The server speaks protocol **2026-07-28** (stateless, `server/discover`)
and still answers clients using the older `initialize` handshake (2025-11-25), such as current VS Code.
Every tool declares MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint: false`) so clients can auto-approve the read-only tools and prompt only for edits.
Within one MCP server process, live UNO operations are serialized: concurrent requests queue
rather than edit simultaneously. Use explicit document paths when working on several files;
the lock does not coordinate separate server processes or manual user edits.

## Requirements

- **LibreOffice 7.x or newer.**
  - Windows and macOS: the bundled Python includes UNO.
  - Debian/Ubuntu: `sudo apt install libreoffice python3-uno`.
- **Python 3.12+ and [uv](https://docs.astral.sh/uv/).**

See [docs/PREREQUISITES.md](docs/PREREQUISITES.md).

## Install

```bash
git clone <this repository> mcp-libre
cd mcp-libre
uv sync --locked
uv run --locked python src/main.py --check   # reports discovery and connection; never starts LibreOffice
```

## Configure your MCP client

**VS Code** (`.vscode/mcp.json` or the workspace `.mcp.json`):

```json
{
  "servers": {
    "libreoffice": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--locked", "--directory", "C:\\path\\to\\mcp-libre", "python", "src/main.py"],
      "env": { "LIBREMCP_APPS": "writer,calc,impress,math,base" }
    }
  }
}
```

**Claude Desktop:** use [config/claude_config.json.template](config/claude_config.json.template), or
run `./generate-config.sh`.

### How it connects

On the first call the server connects to the pipe (default `libremcp_<user>`).

- **LibreOffice is not listening yet:** the server runs
  `soffice --accept=pipe,name=<pipe>;urp; --norestore`.
  - If LibreOffice is already open, the request is forwarded to that instance and your open
    documents stay as they are.
  - Otherwise LibreOffice starts.
- **To never launch LibreOffice automatically:** set `LIBREMCP_AUTOSTART=0` and start it yourself
  with `soffice --accept="pipe,name=libremcp_<user>;urp;"`.

## Tools

Each document-editing tool takes an optional `doc` argument: a title, file name, full path or index
from `list_open_documents`. If you leave it out, the tool works on the active document.

| Group | Tools |
|-------|-------|
| Shared | `list_open_documents`, `open_document`, `save_document`, `render_page` (returns a PNG of a page, slide, sheet or formula), `run_uno` (catch-all) |
| Closed files | `read_document_text`, `convert_document` (headless, using an isolated temporary profile) |
| Writer | `writer_get_paragraphs`, `writer_find_paragraphs`, `writer_format_paragraphs`, `writer_insert_paragraphs`, `writer_replace_text` |
| Calc | `calc_list_sheets`, `calc_get_range`, `calc_set_range`, `calc_format_range`, `calc_find` |
| Impress / Draw | `impress_list_slides`, `impress_get_slide`, `impress_set_shape_text`, `impress_add_slide`, `impress_delete_slide` |
| Math | `math_get_formula`, `math_set_formula` |
| Base | `base_list_tables`, `base_describe_table`, `base_query` (read-only) |

**`writer_format_paragraphs`** is the main formatting tool.
- `copy_from` copies a paragraph that already looks right: its paragraph style, list, indents,
  spacing, alignment and font. Inherited values stay inherited.
- `reset_indents` removes direct indent overrides, the usual cause of misaligned bullets.
- Explicit options (fonts, lists, mm indents and spacing, `extra` raw UNO properties) override
  anything else.

**`run_uno`** runs pyuno Python inside LibreOffice for anything the other tools don't cover. The
names `uno`, `ctx`, `smgr`, `desktop`, `doc`, `pv(**props)` and `filter_data(**props)` are
pre-bound. Assign to `result` to return JSON.

### Agent skill

[skills/libreoffice-mcp/SKILL.md](skills/libreoffice-mcp/SKILL.md) gives AI agents an efficient
workflow. A typical formatting task is about 5 calls: list, find, format with `copy_from`, render,
save. Install it in your agent's skills folder, for example `~/.copilot/skills/libreoffice-mcp/`.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `LIBREMCP_APPS` | `writer,calc,impress,math,base` | Which app tool groups to expose. Fewer tools means less context for the model. |
| `LIBREMCP_ENABLE_RUN_UNO` | `1` | Set to `0` to hide `run_uno`. |
| `LIBREMCP_PIPE` | `libremcp_<user>` | UNO pipe name. |
| `LIBREMCP_AUTOSTART` | `1` | Set to `0` so LibreOffice is never launched or asked to accept connections automatically. |
| `LIBREMCP_OFFICE_PROGRAM` | auto | LibreOffice `program` directory. |
| `LIBREMCP_OFFICE_PYTHON` | auto | A Python interpreter that can `import uno`. |
| `LIBREMCP_TIMEOUT` | `120` | Seconds allowed per call. |

## Security

- **`run_uno` executes arbitrary code** inside LibreOffice with your privileges. If you don't
  need it, disable it with `LIBREMCP_ENABLE_RUN_UNO=0`.
- **Approvals:** keep per-call approval for `run_uno`, `save_document` and `convert_document`.
  Auto-approving the read-only tools avoids approval fatigue. Those are `list_open_documents`,
  `*_get_*`, `*_find*`, `*_list_*`, `render_page` and `read_document_text`.
- The UNO connection is a **local named pipe**; nothing listens on the network.
- **Document text is untrusted input.** Don't let an agent act on instructions found inside
  documents or web pages. Don't connect this server to browser-based chat bridges.
- The legacy LibreOffice extension in [plugin/](plugin/) is **superseded and should not be
  installed**: it runs an unauthenticated HTTP server on all interfaces.

## Testing

```bash
uv run --locked pytest
```

- `tests/test_server_startup.py` checks the tool list, app filtering and file conversion.
- `tests/test_live_tools.py` starts its **own** headless LibreOffice with a temporary profile and a
  unique pipe, then exercises Writer, Calc, Impress, Math and Base on synthetic documents. It never
  touches your LibreOffice session. Concurrent requests on three files (ODT, DOCX and ODS) are
  tested through both protocol modes, including save/reopen, formatting and formula preservation,
  document isolation and a failed request alongside successful reads.

## Documentation

- [docs/LIVE_VIEWING_GUIDE.md](docs/LIVE_VIEWING_GUIDE.md): how live editing works
- [docs/QUICK_START.md](docs/QUICK_START.md), [docs/EXAMPLES.md](docs/EXAMPLES.md)
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- [docs/REPOSITORY_STRUCTURE.md](docs/REPOSITORY_STRUCTURE.md)

## License

This project is licensed under the MIT License; see [LICENSE](LICENSE). For other options see
[docs/LICENSE_OPTIONS.md](docs/LICENSE_OPTIONS.md).

## Links

- MCP specification: https://spec.modelcontextprotocol.io/
- LibreOffice API (UNO): https://api.libreoffice.org/
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
