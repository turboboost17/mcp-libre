# Examples

The examples show tool calls as `tool(arguments)` and assume the document is already open in
LibreOffice. When `doc` is left out, the tool works on the active document.

## Writer: make a section match the rest of the document

```text
writer_find_paragraphs(pattern="Experience")
  -> {"matches": [{"index": 40, "text": "Experience"}]}
writer_get_paragraphs(start=40, count=25)
  -> paragraph 31 is a well-formatted bullet; paragraphs 44-52 have
     "direct_indent": ["ParaLeftMargin", "ParaFirstLineIndent"]
writer_format_paragraphs(indices=["44-52"], copy_from=31, reset_indents=True)
writer_format_paragraphs(indices=[43], copy_from=30)          # the role/date line
render_page(page=2)
save_document()
```

Other Writer operations:

```text
writer_format_paragraphs(indices=[0], font_size=18, bold=True, align="center", space_after_mm=4)
writer_format_paragraphs(indices=["5-9"], list_style="List 1", list_level=0)
writer_insert_paragraphs(after_index=52, texts=["New bullet one", "New bullet two"])
writer_replace_text(search="(\\d{4})-(\\d{4})", replace="$1–$2", regex=True)
```

## Calc

```text
calc_set_range(start="A1", values=[["Item","Qty","Price","Total"],
                                   ["Widget",3,2.5,"=B2*C2"],
                                   ["Gadget",2,4,"=B3*C3"]])
calc_format_range(range="A1:D1", bold=True, background="DDDDDD", autofit_columns=True)
calc_format_range(range="C2:D3", number_format="#,##0.00")
calc_get_range(range="A1:D3")
calc_find(text="gadget")
```

## Impress / Draw

```text
impress_list_slides()
impress_add_slide(after=2, title="Results", body=["Faster", "Smaller", "Cheaper"])
impress_get_slide(slide=3)                       -> shapes with index, type, text
impress_set_shape_text(slide=3, shape_index=1, text=["Faster builds", "Smaller images"])
render_page(page=3)
```

## Math

```text
math_set_formula(formula="x = {-b +- sqrt{b^2 - 4ac}} over {2a}")
render_page()
```

## Base (read-only)

```text
base_list_tables(doc="inventory.odb")
base_describe_table(doc="inventory.odb", table="parts")
base_query(doc="inventory.odb", sql='SELECT "name", "qty" FROM "parts" WHERE "qty" < 5')
```

## Anything else: `run_uno`

`run_uno` can do anything the other tools can't. Batch the whole change into one script, and
assign a value to `result` to return data.

```python
# Set page margins to 20 mm on the default page style
style = doc.StyleFamilies.getByName("PageStyles").getByName("Default Page Style")
for side in ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin"):
    setattr(style, side, 2000)          # 1/100 mm
result = {"margins_mm": 20}
```

## Closed files

```text
read_document_text(path="C:\\docs\\report.docx")
convert_document(source_path="C:\\docs\\report.odt", target_path="C:\\docs\\report.pdf", target_format="pdf")
```
