#!/usr/bin/env python
"""Build the final report .docx from docs/report/report.md.

Usage (from the repository root):
    .venv/Scripts/python.exe docs/report/build_report.py
    .venv/Scripts/python.exe docs/report/build_report.py --out docs/report/MyTeam_Final_Project_Report.docx

Supported Markdown subset (deliberately small and predictable):
  * `<!-- titlepage:start -->` ... `<!-- titlepage:end -->`  centred title page
  * `<!-- pagebreak -->`                                      explicit page break
  * `#` .. `####` headings                                     Heading 1..4
  * paragraphs, `- ` bullets, `1. ` numbered lists
  * `> ` notes (italic, indented)
  * fenced code blocks (```), rendered in a monospace paragraph
  * pipe tables with a `|---|` separator row; a preceding `Table N: ...` line is
    treated as the table caption
  * `![caption](relative/path.png)` images, embedded with an auto-numbered caption
  * inline `**bold**`, `*italic*`, `` `code` ``
Missing images are reported on stderr and rendered as a visible placeholder, so a
skeleton report always builds.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE / "report.md"
DEFAULT_OUTPUT = HERE / "TeamName_Final_Project_Report.docx"

NAVY = RGBColor(0x1F, 0x3A, 0x5F)
BLUE = RGBColor(0x2E, 0x6D, 0xA4)
GREY = RGBColor(0x5B, 0x67, 0x70)
MAX_IMAGE_WIDTH = Inches(6.5)

INLINE_RE = re.compile(r"(\*\*.+?\*\*|`.+?`|\*[^*\n]+?\*)")


# --------------------------------------------------------------------------- #
# document setup
# --------------------------------------------------------------------------- #
def configure_styles(doc: Document) -> None:
    styles = doc.styles

    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.08

    for name, size, color, before, after in (
        ("Heading 1", 16, NAVY, 18, 6),
        ("Heading 2", 13, BLUE, 12, 4),
        ("Heading 3", 11.5, NAVY, 10, 3),
        ("Heading 4", 11, GREY, 8, 2),
    ):
        st = styles[name]
        st.font.name = "Calibri"
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = color
        st.paragraph_format.space_before = Pt(before)
        st.paragraph_format.space_after = Pt(after)
        st.paragraph_format.keep_with_next = True

    title = styles["Title"]
    title.font.name = "Calibri"
    title.font.size = Pt(26)
    title.font.bold = True
    title.font.color.rgb = NAVY
    title.paragraph_format.space_before = Pt(120)
    title.paragraph_format.space_after = Pt(6)

    subtitle = styles["Subtitle"]
    subtitle.font.name = "Calibri"
    subtitle.font.size = Pt(14)
    subtitle.font.italic = True
    subtitle.font.color.rgb = GREY
    subtitle.paragraph_format.space_after = Pt(24)

    caption = styles["Caption"]
    caption.font.name = "Calibri"
    caption.font.size = Pt(9)
    caption.font.italic = True
    caption.font.color.rgb = GREY
    caption.paragraph_format.space_after = Pt(12)

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)


def add_footer(doc: Document, text: str) -> None:
    footer = doc.sections[0].footer
    p = footer.paragraphs[0]
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text + "  ·  page ")
    run.font.size = Pt(8.5)
    run.font.color.rgb = GREY

    def field_run(element) -> None:
        r = p.add_run()
        r.font.size = Pt(8.5)
        r.font.color.rgb = GREY
        r._r.append(element)

    field_begin = OxmlElement("w:fldChar")
    field_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    field_end = OxmlElement("w:fldChar")
    field_end.set(qn("w:fldCharType"), "end")
    for element in (field_begin, instr, field_end):
        field_run(element)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def add_runs(paragraph, text: str, size: Pt | None = None, bold: bool = False,
             color: RGBColor | None = None) -> None:
    """Add text to a paragraph, honouring **bold**, *italic* and `code`."""
    for token in INLINE_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            run = paragraph.add_run(token[2:-2])
            run.bold = True
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            run.font.name = "Consolas"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
            run.font.size = Pt((size or Pt(11)).pt - 1)
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            run.italic = True
        else:
            run = paragraph.add_run(token)
        if size is not None and not token.startswith("`"):
            run.font.size = size
        if bold:
            run.bold = True
        if color is not None:
            run.font.color.rgb = color


def shade(cell, hex_fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def set_table_layout(table, ncols: int) -> None:
    """Keep rows on one page and give each table a usable fixed geometry.

    Word/LibreOffice otherwise autofit every column to the same width and let a
    row split across a page boundary, which makes the wide evidence tables hard
    to read.  Widths are proportional to the content type; the total is 6.5 in,
    matching the page text width.
    """
    profiles = {
        2: [1.25, 5.25],
        3: [1.35, 1.75, 3.40],
        4: [1.05, 1.55, 1.85, 2.05],
        5: [0.40, 0.95, 1.20, 2.95, 1.00],
        6: [0.35, 0.80, 1.00, 1.10, 1.70, 1.55],
    }
    widths = profiles.get(ncols, [6.5 / ncols] * ncols)
    scale = 6.5 / sum(widths)
    widths = [w * scale for w in widths]

    table.autofit = False
    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    tbl_w = OxmlElement("w:tblW")
    tbl_w.set(qn("w:w"), str(int(6.5 * 1440)))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_pr.append(tbl_w)

    for row in table.rows:
        tr_pr = row._tr.get_or_add_trPr()
        cant_split = OxmlElement("w:cantSplit")
        tr_pr.append(cant_split)
        for idx, width in enumerate(widths):
            row.cells[idx].width = Inches(width)


def set_cell_text(cell, text: str, bold: bool = False) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.line_spacing = 1.0
    add_runs(paragraph, text.strip(), size=Pt(9), bold=bold)


def add_code_block(doc: Document, lines: list[str]) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.left_indent = Inches(0.2)
    run = paragraph.add_run("\n".join(lines))
    run.font.name = "Consolas"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
    run.font.size = Pt(8.5)
    p_pr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), "F2F4F7")
    p_pr.append(shd)


def is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped.startswith("|") and re.fullmatch(r"\|[\s:\-|]+\|", stripped))


BLOCK_START = re.compile(r"^(#{1,4}\s|[-*]\s|\d+\.\s|>|\||```|!\[|<!--)")


def join_continuation(lines: list[str], idx: int, first_line: str) -> tuple[str, int]:
    """Join an indented continuation of a list item and return (text, next_index)."""
    parts = [first_line]
    idx += 1
    while idx < len(lines):
        raw = lines[idx]
        if not raw.strip() or not raw[:1].isspace():
            break
        nxt = raw.strip()
        if BLOCK_START.match(nxt):
            break
        parts.append(nxt)
        idx += 1
    return " ".join(parts), idx


def split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #
def build(source: Path, output: Path) -> dict[str, int]:
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()

    doc = Document()
    configure_styles(doc)
    add_footer(doc, "CSCE 4355 — Database Administration · Final Project Report")

    stats = {"headings": 0, "tables": 0, "images": 0, "paragraphs": 0, "missing_images": 0}
    figure_no = 0
    i = 0
    in_titlepage = False
    titlepage_first_heading = True

    def new_paragraph(style: str | None = None, align=None):
        p = doc.add_paragraph(style=style)
        if align is not None:
            p.alignment = align
        stats["paragraphs"] += 1
        return p

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        # ---- directives ----
        if stripped == "<!-- titlepage:start -->":
            in_titlepage = True
            titlepage_first_heading = True
            i += 1
            continue
        if stripped == "<!-- titlepage:end -->":
            in_titlepage = False
            i += 1
            continue
        if stripped == "<!-- pagebreak -->":
            doc.add_page_break()
            i += 1
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            i += 1
            continue

        # ---- blank line ----
        if not stripped:
            i += 1
            continue

        # ---- fenced code ----
        if stripped.startswith("```"):
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1  # closing fence
            add_code_block(doc, block)
            continue

        # ---- headings ----
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level, heading = len(m.group(1)), m.group(2).strip()
            if in_titlepage and titlepage_first_heading:
                p = doc.add_paragraph(style="Title")
                add_runs(p, heading)
                titlepage_first_heading = False
            elif in_titlepage and level == 2 and not any(
                p.style.name.startswith("Heading") for p in doc.paragraphs
            ):
                p = doc.add_paragraph(style="Subtitle")
                add_runs(p, heading)
            else:
                p = doc.add_paragraph(style=f"Heading {min(level, 4)}")
                add_runs(p, heading)
                stats["headings"] += 1
            i += 1
            continue

        # ---- image ----
        m = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", stripped)
        if m:
            caption, rel_path = m.group(1), m.group(2)
            image_path = (source.parent / rel_path).resolve()
            figure_no += 1
            if image_path.exists():
                try:
                    doc.add_picture(str(image_path), width=MAX_IMAGE_WIDTH)
                    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                    stats["images"] += 1
                except Exception as exc:  # pragma: no cover - defensive
                    print(f"WARNING: could not embed {image_path}: {exc}", file=sys.stderr)
                    p = new_paragraph()
                    p.add_run(f"[IMAGE EMBED FAILED: {rel_path}]").italic = True
                    stats["missing_images"] += 1
            else:
                print(f"WARNING: missing image {rel_path} (caption: {caption})", file=sys.stderr)
                p = new_paragraph(align=WD_ALIGN_PARAGRAPH.CENTER)
                run = p.add_run(f"[MISSING FIGURE: {rel_path}]")
                run.italic = True
                run.font.color.rgb = RGBColor(0xB3, 0x5C, 0x00)
                stats["missing_images"] += 1

            cap = doc.add_paragraph(style="Caption")
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap.paragraph_format.keep_with_next = False
            text_caption = caption if re.match(r"^Figure\s+\d+", caption) else f"Figure {figure_no}: {caption}"
            add_runs(cap, text_caption, size=Pt(9), color=GREY)
            i += 1
            continue

        # ---- table ----
        if stripped.startswith("|") and i + 1 < len(lines) and is_table_separator(lines[i + 1]):
            header = split_row(stripped)
            i += 2
            body: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(split_row(lines[i]))
                i += 1

            table = doc.add_table(rows=1, cols=len(header))
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            set_table_layout(table, len(header))
            hdr = table.rows[0]
            for idx, cell_text in enumerate(header):
                set_cell_text(hdr.cells[idx], cell_text, bold=True)
                shade(hdr.cells[idx], "E8EEF7")
            repeat_header(hdr)
            for row_values in body:
                cells = table.add_row().cells
                for idx in range(len(header)):
                    value = row_values[idx] if idx < len(row_values) else ""
                    set_cell_text(cells[idx], value)
            stats["tables"] += 1
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(2)
            continue

        # ---- lists ----
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            item_text, i = join_continuation(lines, i, m.group(1))
            p = new_paragraph(style="List Bullet")
            add_runs(p, item_text)
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if m:
            # Literal numbering with a hanging indent: Word's "List Number" style
            # shares one numbering instance, so separate lists would continue
            # counting instead of restarting at 1.
            item_text, i = join_continuation(lines, i, m.group(2))
            p = new_paragraph()
            p.paragraph_format.left_indent = Inches(0.32)
            p.paragraph_format.first_line_indent = Inches(-0.32)
            p.paragraph_format.space_after = Pt(4)
            p.add_run(f"{m.group(1)}.  ")
            add_runs(p, item_text)
            continue

        # ---- table caption ----
        if re.match(r"^Table\s+\d+\s*:", stripped):
            cap = doc.add_paragraph(style="Caption")
            cap.paragraph_format.keep_with_next = True
            add_runs(cap, stripped, size=Pt(9), color=GREY)
            i += 1
            continue

        # ---- blockquote ----
        if stripped.startswith(">"):
            p = new_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            run = p.add_run(stripped.lstrip("> ").strip())
            run.italic = True
            run.font.size = Pt(10)
            run.font.color.rgb = GREY
            i += 1
            continue

        # ---- horizontal rule ----
        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            i += 1
            continue

        # ---- title page line ----
        if in_titlepage:
            p = new_paragraph(align=WD_ALIGN_PARAGRAPH.CENTER)
            add_runs(p, stripped, size=Pt(12), color=GREY)
            i += 1
            continue

        # ---- paragraph (join wrapped lines) ----
        chunk = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("#") or nxt.startswith("|") or nxt.startswith("```")
                    or nxt.startswith("![") or nxt.startswith("<!--")
                    or re.match(r"^[-*]\s+", nxt) or re.match(r"^\d+\.\s+", nxt)
                    or nxt.startswith(">")):
                break
            chunk.append(nxt)
            i += 1
        p = new_paragraph()
        add_runs(p, " ".join(chunk))

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    stats = build(args.source, args.out)

    from docx import Document as _Document
    check = _Document(str(args.out))
    style_counts: dict[str, int] = {}
    for p in check.paragraphs:
        style_counts[p.style.name] = style_counts.get(p.style.name, 0) + 1
    headings = sum(v for k, v in style_counts.items() if k.startswith("Heading"))
    print(f"source      : {args.source}")
    print(f"output      : {args.out}")
    print(f"paragraphs  : {len(check.paragraphs)}")
    print(f"headings    : {headings} {dict(sorted(style_counts.items())) if headings else ''}")
    print(f"tables      : {len(check.tables)}")
    print(f"images      : {len(check.inline_shapes)}")
    print(f"missing imgs: {stats['missing_images']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
