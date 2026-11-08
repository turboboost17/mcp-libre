# Quick Start

1. **Install the prerequisites:** LibreOffice and uv. See [PREREQUISITES.md](PREREQUISITES.md).
2. **Install the server:**

   ```bash
   cd mcp-libre
   uv sync --locked
   uv run --locked python src/main.py --check
   ```

3. **Add the server to your MCP client.**
   - VS Code: use the snippet in the [README](../README.md#configure-your-mcp-client).
   - Claude Desktop: run `./generate-config.sh`.
4. **Optional: install the agent skill.** Copy
   [skills/libreoffice-mcp/SKILL.md](../skills/libreoffice-mcp/SKILL.md) into your agent's skills
   folder. It shows the agent how to finish common tasks in a handful of calls.
5. **Open a document in LibreOffice** and ask the assistant, for example:
   - "Make the bullets in the Experience section of my open document match the section above it."
   - "In the open spreadsheet, bold the header row and format column C as currency."
   - "Add a slide after slide 2 titled 'Results' with three bullet points."

The first request connects to your running LibreOffice. You'll see changes appear in the window,
and Ctrl+Z undoes a whole tool call. Nothing is saved until you, or the assistant through
`save_document`, saves the document.

## Recommended client settings

- Auto-approve the read-only tools: `list_open_documents`, `*_get_*`, `*_find*`, `*_list_*`,
  `render_page` and `read_document_text`.
- Keep approval on for `run_uno`, `save_document` and `convert_document`.
- To give the model fewer tools, set `LIBREMCP_APPS`. For example, use `writer` if you only edit
  text documents.
