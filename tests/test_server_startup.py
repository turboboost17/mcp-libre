import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import libremcp  # noqa: E402

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


def _client(env=None, mode="auto"):
    return Client(StdioServerParameters(command=sys.executable, args=[str(ENTRY_POINT)], env=env), mode=mode)


async def _list_tools(env=None, mode="auto"):
    async with asyncio.timeout(30):
        async with _client(env, mode) as client:
            return client.protocol_version, (await client.list_tools()).tools


# "legacy" = 2025-era initialize handshake (current VS Code / Copilot); "auto" = 2026-07-28 discover.
@pytest.mark.asyncio
@pytest.mark.parametrize("mode, version", [("legacy", "2025-11-25"), ("auto", "2026-07-28")])
async def test_stdio_server_lists_exactly_the_expected_tools(mode, version):
    negotiated, tools = await _list_tools(mode=mode)
    assert negotiated == version
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_tools_carry_behaviour_annotations():
    assert libremcp.READ_ONLY_TOOLS | libremcp.DESTRUCTIVE_TOOLS | libremcp.IDEMPOTENT_TOOLS <= EXPECTED_TOOLS
    _, tools = await _list_tools()
    by_name = {tool.name: tool.annotations for tool in tools}
    for name, ann in by_name.items():
        assert ann is not None and ann.open_world_hint is False, name
        assert ann.read_only_hint is (name in libremcp.READ_ONLY_TOOLS), name
    assert by_name["writer_get_paragraphs"].read_only_hint
    assert by_name["run_uno"].destructive_hint and by_name["save_document"].destructive_hint
    assert by_name["writer_format_paragraphs"].destructive_hint is False


@pytest.mark.asyncio
async def test_app_groups_and_run_uno_can_be_disabled():
    env = {**os.environ, "LIBREMCP_APPS": "writer", "LIBREMCP_ENABLE_RUN_UNO": "0"}
    _, tools = await _list_tools(env)
    names = {tool.name for tool in tools}
    assert "writer_format_paragraphs" in names
    assert "run_uno" not in names
    assert not any(name.startswith(("calc_", "impress_", "math_", "base_")) for name in names)


@pytest.mark.asyncio
async def test_file_tools_convert_and_read_closed_documents(tmp_path):
    source = tmp_path / "synthetic.txt"
    source.write_text("MCP smoke test document.\nTemporary test content only.\n", encoding="utf-8")
    odt = tmp_path / "synthetic.odt"
    pdf = tmp_path / "synthetic.pdf"

    async with asyncio.timeout(180):
        async with _client() as client:
            for target, fmt in ((odt, "odt"), (pdf, "pdf")):
                src = source if fmt == "odt" else odt
                converted = await client.call_tool(
                    "convert_document",
                    {"source_path": str(src), "target_path": str(target), "target_format": fmt},
                )
                assert not converted.is_error, converted.content
                assert converted.structured_content["success"], converted.structured_content
            read = await client.call_tool("read_document_text", {"path": str(odt)})
            assert not read.is_error, read.content
            assert "Temporary test content only." in read.structured_content["content"]

    assert pdf.read_bytes().startswith(b"%PDF-")
