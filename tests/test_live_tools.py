"""End-to-end tests for the live (UNO) tools against an isolated headless LibreOffice.

A private LibreOffice instance with a throw-away profile and a unique pipe name is started, so a
user's running LibreOffice and open documents are never touched. Only synthetic content is used.
"""

import asyncio
import base64
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import libremcp  # noqa: E402

PIPE = f"libremcp_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def office(tmp_path_factory):
    soffice = libremcp._soffice_gui()
    if libremcp._office_program_dir() is None:
        pytest.skip("LibreOffice is not installed")
    profile = tmp_path_factory.mktemp("lo_profile")
    proc = subprocess.Popen(
        [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--invisible",
         "--norestore", "--nologo", "--nodefault", f"--accept=pipe,name={PIPE};urp;"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {**os.environ, "LIBREMCP_PIPE": PIPE, "LIBREMCP_AUTOSTART": "0"}
    yield env
    request = {"op": "run_uno", "args": {"script": "desktop.terminate()", "undo_group": False},
               "pipe": PIPE, "connect_timeout": 5}
    try:
        libremcp._invoke_bridge(request)
    except Exception:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _session(env, body):
    server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "src" / "main.py")], env=env)
    async with asyncio.timeout(300):
        async with Client(server) as client:

            async def call(name, **args):
                result = await client.call_tool(name, args)
                assert not result.is_error, (name, result.content)
                content = result.structured_content
                if content is None:
                    return result
                return content["result"] if set(content) == {"result"} else content

            return await body(call)


def _wait_for_office(env):
    request = {"op": "list_open_documents", "args": {}, "pipe": env["LIBREMCP_PIPE"], "connect_timeout": 60}
    response = libremcp._invoke_bridge(request)
    assert response["ok"], response


def _image(result):
    images = [c for c in result.content if c.type == "image"]
    assert images and images[0].mime_type == "image/png"
    assert base64.b64decode(images[0].data).startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_writer_formatting_workflow(office, tmp_path):
    _wait_for_office(office)
    saved = tmp_path / "synthetic_cv.odt"

    async def body(call):
        opened = await call("open_document", new_type="writer", hidden=True)
        doc = opened["title"]
        setup = r'''
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
text = doc.Text
cursor = text.createTextCursor()
lines = ["Experience", "Example Corp - Engineer", "Built test fixtures for widgets.",
         "Maintained the widget pipeline.", "Sample Org - Senior Engineer",
         "Led a synthetic project with a fairly long description that wraps onto a second line in the page.",
         "Improved imaginary metrics.", "Reviewed placeholder designs."]
for i, line in enumerate(lines):
    if i:
        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    text.insertString(cursor, line, False)
result = sorted(doc.StyleFamilies.getByName("NumberingStyles").ElementNames)
'''
        styles = (await call("run_uno", script=setup, doc=doc))["result"]
        bullet = "List 1" if "List 1" in styles else styles[0]

        await call("writer_format_paragraphs", doc=doc, indices=["2-3"], list_style=bullet,
                   font_name="Liberation Sans", font_size=11)
        await call("writer_format_paragraphs", doc=doc, indices=[6], left_indent_mm=3,
                   first_line_indent_mm=0, font_name="Liberation Serif")

        found = await call("writer_find_paragraphs", doc=doc, pattern="sample org")
        assert [m["index"] for m in found["matches"]] == [4]

        before = await call("writer_get_paragraphs", doc=doc)
        assert before["total"] == 8
        reference = before["paragraphs"][2]
        assert reference["list"]["style"] == bullet
        assert "direct_indent" in before["paragraphs"][6]
        original_text = [p["text"] for p in before["paragraphs"]]

        await call("writer_format_paragraphs", doc=doc, indices=["5-7"], copy_from=2, reset_indents=True)
        await call("writer_format_paragraphs", doc=doc, indices=[4], copy_from=1, bold=True)

        after = await call("writer_get_paragraphs", doc=doc)
        assert [p["text"] for p in after["paragraphs"]] == original_text
        for p in after["paragraphs"][5:8]:
            assert p["list"]["style"] == bullet
            assert p["font"]["font"] == "Liberation Sans" and p["font"]["size"] == 11
            assert p["indent_mm"] == reference["indent_mm"]
            assert "direct_indent" not in p
        assert after["paragraphs"][4]["font"]["bold"] is True

        inserted = await call("writer_insert_paragraphs", doc=doc, after_index=7, texts=["Added line."])
        assert inserted["inserted"] == [8]
        assert inserted["result"][0]["list"]["style"] == bullet

        replaced = await call("writer_replace_text", doc=doc, search="imaginary", replace="hypothetical")
        assert replaced["replacements"] == 1

        _image(await call("render_page", doc=doc, page=1, width=600))
        await call("save_document", doc=doc, path=str(saved))
        await call("run_uno", doc=str(saved), script="doc.close(True)", undo_group=False)

    await _session(office, body)
    text = await _session(office, lambda call: call("read_document_text", path=str(saved)))
    assert "hypothetical metrics" in text["content"]


@pytest.mark.asyncio
async def test_calc_tools(office):
    _wait_for_office(office)

    async def body(call):
        doc = (await call("open_document", new_type="calc", hidden=True))["title"]
        written = await call("calc_set_range", doc=doc, start="A1",
                             values=[["Item", "Qty", "Price"], ["Widget", 3, 2.5], ["Gadget", 2, 4], ["Total", None, "=B2*C2+B3*C3"]])
        assert written["range"] == "A1:C4"
        values = await call("calc_get_range", doc=doc, range="A1:C4")
        assert values["values"][3][2] == 15.5
        formulas = await call("calc_get_range", doc=doc, range="C4", formulas=True)
        assert formulas["values"] == [["=B2*C2+B3*C3"]]
        fmt = await call("calc_format_range", doc=doc, range="A1:C1", bold=True, background="DDDDDD",
                         autofit_columns=True)
        assert "CharWeight" in fmt["applied"]
        await call("calc_format_range", doc=doc, range="C2:C4", number_format="#,##0.00")
        found = await call("calc_find", doc=doc, text="gadget")
        assert found["matches"][0]["range"] == "A3"
        sheets = await call("calc_list_sheets", doc=doc)
        assert sheets["sheets"][0]["used_range"] == "A1:C4"
        _image(await call("render_page", doc=doc, page=1))
        await call("run_uno", doc=doc, script="doc.close(True)", undo_group=False)

    await _session(office, body)


@pytest.mark.asyncio
async def test_impress_and_math_tools(office):
    _wait_for_office(office)

    async def body(call):
        doc = (await call("open_document", new_type="impress", hidden=True))["title"]
        added = await call("impress_add_slide", doc=doc, after=1, title="Synthetic title",
                           body=["First point", "Second point"])
        assert added["slide"] == 2
        slides = await call("impress_list_slides", doc=doc)
        assert slides["slides"][1]["title"] == "Synthetic title"
        shapes = (await call("impress_get_slide", doc=doc, slide=2))["shapes"]
        body_shape = next(s for s in shapes if s["type"] == "OutlinerShape")
        assert body_shape["text"] == "First point\nSecond point"
        changed = await call("impress_set_shape_text", doc=doc, slide=2,
                             shape_index=body_shape["index"], text=["Only point"])
        assert changed["text"] == "Only point"
        _image(await call("render_page", doc=doc, page=2, width=640))
        deleted = await call("impress_delete_slide", doc=doc, slide=1)
        assert deleted["count"] == 1
        await call("run_uno", doc=doc, script="doc.close(True)", undo_group=False)

        math = (await call("open_document", new_type="math", hidden=True))["title"]
        await call("math_set_formula", doc=math, formula="a^2 + b^2 = c^2")
        assert (await call("math_get_formula", doc=math))["formula"] == "a^2 + b^2 = c^2"
        _image(await call("render_page", doc=math, width=400))
        await call("run_uno", doc=math, script="doc.close(True)", undo_group=False)

    await _session(office, body)


@pytest.mark.asyncio
async def test_base_tools(office, tmp_path):
    _wait_for_office(office)
    odb = tmp_path / "synthetic.odb"
    create = r'''
import os
path = r"%s"
dbc = smgr.createInstanceWithContext("com.sun.star.sdb.DatabaseContext", ctx)
source = dbc.createInstance()
source.URL = "sdbc:embedded:firebird"
source.DatabaseDocument.storeAsURL(uno.systemPathToFileUrl(path), ())
conn = source.getConnection("", "")
stmt = conn.createStatement()
stmt.executeUpdate('CREATE TABLE "people" ("id" INTEGER PRIMARY KEY, "name" VARCHAR(40))')
stmt.executeUpdate("INSERT INTO \"people\" VALUES (1, 'Alpha')")
stmt.executeUpdate("INSERT INTO \"people\" VALUES (2, 'Beta')")
conn.commit()
conn.close()
source.DatabaseDocument.store()
source.DatabaseDocument.close(True)
result = os.path.exists(path)
''' % odb

    async def body(call):
        try:
            assert (await call("run_uno", script=create, undo_group=False))["result"] is True
        except AssertionError as exc:
            pytest.skip(f"Embedded Firebird unavailable: {exc}")
        doc = (await call("open_document", path=str(odb), hidden=True))["title"]
        tables = await call("base_list_tables", doc=doc)
        assert "people" in tables["tables"]
        columns = await call("base_describe_table", doc=doc, table="people")
        assert [c["name"] for c in columns["columns"]] == ["id", "name"]
        rows = await call("base_query", doc=doc, sql='SELECT "name" FROM "people" ORDER BY "id"')
        assert rows["rows"] == [["Alpha"], ["Beta"]]
        with pytest.raises(AssertionError):
            await call("base_query", doc=doc, sql='DELETE FROM "people"')
        await call("run_uno", doc=doc, script="doc.close(True)", undo_group=False)

    await _session(office, body)
