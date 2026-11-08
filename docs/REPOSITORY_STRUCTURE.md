# Repository Structure

```text
mcp-libre/
├── src/
│   ├── main.py              # entry point (stdio MCP server; --check, --version, --help)
│   ├── libremcp.py          # MCP tool definitions, LibreOffice discovery, bridge launcher
│   └── uno_bridge.py        # runs under LibreOffice's Python; all UNO operations
├── skills/
│   └── libreoffice-mcp/
│       └── SKILL.md         # agent workflow guidance (efficient tool usage)
├── tests/
│   ├── test_server_startup.py   # tool list, app filtering, file conversion
│   └── test_live_tools.py       # Writer/Calc/Impress/Math/Base against a private headless instance
├── config/
│   └── claude_config.json.template
├── scripts/
│   ├── mcp-helper.sh        # check | requirements | test | server | config
│   └── generate-config.sh   # writes a Claude Desktop config from the template
├── docs/                    # guides (this file, quick start, examples, troubleshooting, ...)
├── plugin/                  # legacy LibreOffice extension (superseded, do not install)
├── generate-config.sh, mcp-helper.sh, quick-test.sh   # convenience wrappers
├── pyproject.toml, uv.lock  # dependencies (use `uv sync --locked`)
├── LICENSE
└── README.md
```

## Why two Python interpreters?

The MCP server runs in the project's uv environment, which can't import `uno`. For every
live-document call it starts `uno_bridge.py` under LibreOffice's own Python (or a system Python
that has `python3-uno`). It passes the request as JSON on stdin and reads one JSON result line
back. The bridge connects to the running LibreOffice through the local named pipe.
