# -*- coding: utf-8 -*-
"""把需求说明书 Markdown 生成同名 Word 文档。

只支持文档实际用到的语法：一级/二级/三级标题、段落、表格、
行内粗体与行内代码。Markdown 是唯一来源，Word 由本脚本生成。

用法：
    python docs/build_requirements_docx.py
    python docs/build_requirements_docx.py --src docs/某文档.md --out docs/某文档.docx
"""

import os
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "软件需求说明书.md")
OUT = os.path.join(HERE, "金融AI模型风险监测与审计平台软件需求说明书.docx")

BODY_FONT = "宋体"
HEAD_FONT = "微软雅黑"
CODE_FONT = "Consolas"
BODY_PT = 12

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def set_font(run, name, size=None, bold=None, color=None):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = RGBColor(*color)


def add_inline(paragraph, text, size=BODY_PT):
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            inner = piece[2:-2]
            run = paragraph.add_run(inner.replace("`", ""))
            set_font(run, CODE_FONT if "`" in inner else BODY_FONT, size, bold=True)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            set_font(run, CODE_FONT, size - 1.5)
            run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x93)
        else:
            run = paragraph.add_run(piece)
            set_font(run, BODY_FONT, size)


def body(doc, text, *, indent=True, space_after=6):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = 1.5
    pf.space_after = Pt(space_after)
    pf.widow_control = True
    if indent:
        pf.first_line_indent = Pt(BODY_PT * 2)
    add_inline(p, text)
    return p


def heading(doc, text, level):
    p = doc.add_paragraph(style=f"Heading {level}")
    pf = p.paragraph_format
    pf.space_before = Pt(16 if level == 1 else 10)
    pf.space_after = Pt(8 if level == 1 else 6)
    pf.keep_with_next = True
    run = p.add_run(text)
    set_font(run, HEAD_FONT, {1: 16, 2: 14, 3: 12.5}.get(level, 12), bold=True)
    return p


def _border(cell):
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), "808080")
        borders.append(el)
    tcPr.append(borders)


def table(doc, rows):
    header, body_rows = rows[0], rows[1:]
    t = doc.add_table(rows=1, cols=len(header))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = True
    for i, cell_text in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        add_inline(p, cell_text, size=10.5)
        for run in p.runs:
            run.font.bold = True
        _border(cell)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), "EEF2F7")
        cell._tc.get_or_add_tcPr().append(shd)
    for row in body_rows:
        cells = t.add_row().cells
        for i, cell_text in enumerate(row[:len(header)]):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            add_inline(p, cell_text, size=10.5)
            _border(cells[i])
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def code_line(doc, text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = 1.2
    pf.space_after = Pt(0)
    pf.left_indent = Pt(BODY_PT)
    run = p.add_run(text)
    set_font(run, CODE_FONT, BODY_PT - 1.5)
    run.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)
    return p


def bullet(doc, text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = 1.5
    pf.space_after = Pt(4)
    pf.left_indent = Pt(BODY_PT * 2)
    pf.first_line_indent = Pt(-BODY_PT)
    add_inline(p, "• " + text)
    return p


def build(src=SRC, out=OUT):
    lines = open(src, encoding="utf-8").read().splitlines()
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(3.17)
    section.top_margin = section.bottom_margin = Cm(2.54)

    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            in_code = not in_code
            if not in_code:
                spacer = doc.add_paragraph()
                spacer.paragraph_format.space_after = Pt(2)
            i += 1
            continue
        if in_code:
            code_line(doc, line)
            i += 1
            continue
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = split_row(lines[i])
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                table(doc, rows)
            continue
        if line.startswith("# "):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(line[2:].strip())
            set_font(run, HEAD_FONT, 22, bold=True)
        elif line.startswith("#### "):
            heading(doc, line[5:].strip(), 3)
        elif line.startswith("### "):
            heading(doc, line[4:].strip(), 2)
        elif line.startswith("## "):
            heading(doc, line[3:].strip(), 1)
        elif line.startswith("- "):
            bullet(doc, line[2:].strip())
        else:
            body(doc, line.strip())
        i += 1

    doc.save(out)
    print("saved", out)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="把 Markdown 生成同名结构的 Word 文档")
    parser.add_argument("--src", default=SRC)
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()
    build(args.src, args.out)
