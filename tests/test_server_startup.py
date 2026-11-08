import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ENTRY_POINT = Path(__file__).resolve().parents[1] / "src" / "main.py"

EXPECTED_TOOLS = {
    "list_open_documents", "open_document", "save_document", "render_page", "run_uno",
    "read_document_text", "convert_document",
    "writer_get_paragraphs", "writer_find_paragraphs", "writer_format_paragraphs",
    "writer_insert_paragraphs", "writer_replace_text",
    "calc_list_sheets", "calc_get_range", "calc_set_range", "calc_format_range", "calc_find",
    "impress_list_slides", "impress_get_slide", "impress_set_shape_text", "impress_add_slide",
    "impress_delete_slide",
    "math_get_formula", "math_set_formula",
    "base_list_tables", "base_describe_table", "base_query",
}


async def _list_tools(env=None):
    server = StdioServerParameters(command=sys.executable, args=[str(ENTRY_POINT)], env=env)
    async with asyncio.timeout(30):
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                return {tool.name for tool in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_stdio_server_lists_exactly_the_expected_tools():
    assert await _list_tools() == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_app_groups_and_run_uno_can_be_disabled():
    import os

    env = {**os.environ, "LIBREMCP_APPS": "writer", "LIBREMCP_ENABLE_RUN_UNO": "0"}
    tools = await _list_tools(env)
    assert "writer_format_paragraphs" in tools
    assert "run_uno" not in tools
    assert not any(name.startswith(("calc_", "impress_", "math_", "base_")) for name in tools)


@pytest.mark.asyncio
async def test_file_tools_convert_and_read_closed_documents(tmp_path):
    source = tmp_path / "synthetic.txt"
    source.write_text("MCP smoke test document.\nTemporary test content only.\n", encoding="utf-8")
    odt = tmp_path / "synthetic.odt"
    pdf = tmp_path / "synthetic.pdf"
    server = StdioServerParameters(command=sys.executable, args=[str(ENTRY_POINT)])

    async with asyncio.timeout(180):
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                for target, fmt in ((odt, "odt"), (pdf, "pdf")):
                    src = source if fmt == "odt" else odt
                    converted = await client.call_tool(
                        "convert_document",
                        {"source_path": str(src), "target_path": str(target), "target_format": fmt},
                    )
                    assert not converted.isError, converted.content
                    assert converted.structuredContent["success"], converted.structuredContent
                read = await client.call_tool("read_document_text", {"path": str(odt)})
                assert not read.isError, read.content
                assert "Temporary test content only." in read.structuredContent["content"]

    assert pdf.read_bytes().startswith(b"%PDF-")
