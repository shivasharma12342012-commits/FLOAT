"""Float — turning a reply into a file a teacher can print, mail or upload.

A lesson plan that only exists in a chat window is not a lesson plan. Everything
Float writes can become a real document, and it builds them here: Word, PDF,
Excel, CSV, HTML, Markdown and plain text, all from the standard library.

Why write OOXML and PDF by hand rather than install ``python-docx`` and
``reportlab``. Because this runs on school machines, often behind a proxy that
blocks PyPI, and a first-run that fails at ``pip install`` is a product that does
not work. The subset written here — headings, bold, italic, bullets, numbers,
tables, page breaks — is the subset a school document actually uses.

The input is Markdown, because that is what the model already produces well.
"""

from __future__ import annotations

import csv
import html
import io
import os
import platform
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import config, db

KINDS = {
    "docx": ("Word document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf": ("PDF", "application/pdf"),
    "xlsx": ("Excel sheet", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "csv": ("CSV", "text/csv"),
    "html": ("Web page", "text/html"),
    "md": ("Markdown", "text/markdown"),
    "txt": ("Text", "text/plain"),
}

_SAFE = re.compile(r"[^A-Za-z0-9 ._()\u0900-\u097F-]+")


def safe_name(name: str, extension: str) -> str:
    stem = _SAFE.sub("", (name or "Document").strip())[:80].strip() or "Document"
    if stem.lower().endswith("." + extension):
        stem = stem[: -(len(extension) + 1)]
    return f"{stem}.{extension}"


# ---------------------------------------------------------------------------
# Markdown, parsed just far enough
# ---------------------------------------------------------------------------
@dataclass
class Block:
    kind: str           # h1..h4 | p | bullet | number | table | rule | pagebreak | quote | code
    text: str = ""
    rows: list[list[str]] | None = None
    level: int = 0


_TABLE_SEP = re.compile(r"^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$")


def parse_markdown(source: str) -> list[Block]:
    lines = (source or "").replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        if buffer:
            blocks.append(Block("p", " ".join(buffer).strip()))
            buffer.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        if stripped in {"---", "***", "___"}:
            flush()
            blocks.append(Block("rule"))
            index += 1
            continue

        if stripped == "\\pagebreak" or stripped == "<!-- pagebreak -->":
            flush()
            blocks.append(Block("pagebreak"))
            index += 1
            continue

        if stripped.startswith("```"):
            flush()
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            blocks.append(Block("code", "\n".join(code)))
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush()
            blocks.append(Block(f"h{len(heading.group(1))}", heading.group(2).strip()))
            index += 1
            continue

        if stripped.startswith(">"):
            flush()
            blocks.append(Block("quote", stripped.lstrip("> ").strip()))
            index += 1
            continue

        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet:
            flush()
            blocks.append(Block("bullet", bullet.group(2).strip(), level=len(bullet.group(1)) // 2))
            index += 1
            continue

        number = re.match(r"^(\s*)\d+[.)]\s+(.*)$", line)
        if number:
            flush()
            blocks.append(Block("number", number.group(2).strip(), level=len(number.group(1)) // 2))
            index += 1
            continue

        if "|" in stripped and index + 1 < len(lines) and _TABLE_SEP.match(lines[index + 1]):
            flush()
            rows: list[list[str]] = [_split_row(stripped)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_row(lines[index]))
                index += 1
            width = max(len(r) for r in rows)
            for row in rows:
                row.extend([""] * (width - len(row)))
            blocks.append(Block("table", rows=rows))
            continue

        buffer.append(stripped)
        index += 1

    flush()
    return blocks


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|\*.+?\*|_.+?_|`.+?`)")


def inline_runs(text: str) -> list[tuple[str, set[str]]]:
    """Split text into (fragment, styles) pairs. Styles: bold, italic, code."""
    runs: list[tuple[str, set[str]]] = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if (piece.startswith("**") and piece.endswith("**")) or (
            piece.startswith("__") and piece.endswith("__")
        ):
            runs.append((piece[2:-2], {"bold"}))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 1:
            runs.append((piece[1:-1], {"code"}))
        elif (piece.startswith("*") and piece.endswith("*") and len(piece) > 2) or (
            piece.startswith("_") and piece.endswith("_") and len(piece) > 2
        ):
            runs.append((piece[1:-1], {"italic"}))
        else:
            runs.append((piece, set()))
    return runs or [(text, set())]


def strip_markdown(text: str) -> str:
    return re.sub(r"[*_`#>]", "", text)


# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------
_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>"""


def _docx_styles() -> str:
    def heading(sid: str, name: str, size: int, colour: str, before: int) -> str:
        return (
            f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
            f'<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/>'
            f'<w:spacing w:before="{before}" w:after="120"/></w:pPr>'
            f'<w:rPr><w:b/><w:color w:val="{colour}"/><w:sz w:val="{size}"/></w:rPr></w:style>'
        )

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Nirmala UI"/><w:sz w:val="22"/>
</w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
{heading("Title", "Title", 44, "1E3A32", 0)}
{heading("Heading1", "heading 1", 32, "1E3A32", 320)}
{heading("Heading2", "heading 2", 26, "2C4F44", 280)}
{heading("Heading3", "heading 3", 24, "44605A", 240)}
{heading("Heading4", "heading 4", 22, "44605A", 200)}
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>
<w:pPr><w:ind w:left="480"/></w:pPr><w:rPr><w:i/><w:color w:val="5C6B63"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/>
<w:pPr><w:shd w:val="clear" w:fill="F2F4F1"/><w:ind w:left="240"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="19"/></w:rPr></w:style>
</w:styles>"""


_DOCX_NUMBERING = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>
<w:pPr><w:ind w:left="480" w:hanging="240"/></w:pPr></w:lvl>
<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#9702;"/>
<w:pPr><w:ind w:left="960" w:hanging="240"/></w:pPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
<w:pPr><w:ind w:left="480" w:hanging="240"/></w:pPr></w:lvl>
<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2."/>
<w:pPr><w:ind w:left="960" w:hanging="240"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _docx_runs(text: str) -> str:
    out: list[str] = []
    for fragment, styles in inline_runs(text):
        if not fragment:
            continue
        props = []
        if "bold" in styles:
            props.append("<w:b/>")
        if "italic" in styles:
            props.append("<w:i/>")
        if "code" in styles:
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{_xml(fragment)}</w:t></w:r>')
    return "".join(out) or '<w:r><w:t xml:space="preserve"></w:t></w:r>'


def _docx_body(blocks: Iterable[Block], title: str, subtitle: str) -> str:
    parts: list[str] = []
    if title:
        parts.append(f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>{_docx_runs(title)}</w:p>')
    if subtitle:
        parts.append(
            '<w:p><w:pPr><w:spacing w:after="300"/></w:pPr><w:r><w:rPr><w:color w:val="5C6B63"/>'
            f'<w:sz w:val="20"/></w:rPr><w:t xml:space="preserve">{_xml(subtitle)}</w:t></w:r></w:p>'
        )

    for block in blocks:
        if block.kind.startswith("h"):
            style = f"Heading{block.kind[1]}"
            parts.append(f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{_docx_runs(block.text)}</w:p>')
        elif block.kind == "bullet":
            parts.append(
                f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{min(block.level,1)}"/>'
                f'<w:numId w:val="1"/></w:numPr><w:spacing w:after="60"/></w:pPr>'
                f"{_docx_runs(block.text)}</w:p>"
            )
        elif block.kind == "number":
            parts.append(
                f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{min(block.level,1)}"/>'
                f'<w:numId w:val="2"/></w:numPr><w:spacing w:after="60"/></w:pPr>'
                f"{_docx_runs(block.text)}</w:p>"
            )
        elif block.kind == "quote":
            parts.append(f'<w:p><w:pPr><w:pStyle w:val="Quote"/></w:pPr>{_docx_runs(block.text)}</w:p>')
        elif block.kind == "code":
            for line in block.text.split("\n"):
                parts.append(
                    '<w:p><w:pPr><w:pStyle w:val="Code"/><w:spacing w:after="0"/></w:pPr>'
                    f'<w:r><w:t xml:space="preserve">{_xml(line)}</w:t></w:r></w:p>'
                )
        elif block.kind == "rule":
            parts.append(
                '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:color="D8DED9"/>'
                "</w:pBdr></w:pPr></w:p>"
            )
        elif block.kind == "pagebreak":
            parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        elif block.kind == "table" and block.rows:
            parts.append(_docx_table(block.rows))
        else:
            parts.append(f"<w:p>{_docx_runs(block.text)}</w:p>")

    parts.append(
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(parts)}</w:body></w:document>"
    )


def _docx_table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows) or 1
    cell_width = int(9638 / width)
    body: list[str] = []
    for index, row in enumerate(rows):
        cells: list[str] = []
        for value in row:
            shade = '<w:shd w:val="clear" w:fill="EDF2EE"/>' if index == 0 else ""
            text = f"**{value}**" if index == 0 and value else value
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{cell_width}" w:type="dxa"/>{shade}</w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:after="40"/></w:pPr>{_docx_runs(text)}</w:p></w:tc>'
            )
        body.append(f"<w:tr>{''.join(cells)}</w:tr>")
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:color="D8DED9"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="9638" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders>'
        f"</w:tblPr>{''.join(body)}</w:tbl>"
        '<w:p><w:pPr><w:spacing w:after="120"/></w:pPr></w:p>'
    )


def build_docx(markdown: str, title: str = "", subtitle: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _DOCX_RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/styles.xml", _docx_styles())
        archive.writestr("word/numbering.xml", _DOCX_NUMBERING)
        archive.writestr("word/document.xml", _docx_body(parse_markdown(markdown), title, subtitle))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
_PAGE_W, _PAGE_H = 595.28, 841.89          # A4 in points
_MARGIN = 56.0
_LEADING = 15.0


def _pdf_escape(text: str) -> str:
    out = []
    for char in text:
        if char in "()\\":
            out.append("\\" + char)
        elif ord(char) < 32:
            out.append(" ")
        elif ord(char) < 128:
            out.append(char)
        else:
            out.append("?")   # Latin-1 core fonts only; see build_pdf's note
    return "".join(out)


def _wrap(text: str, size: float, width: float) -> list[str]:
    """Greedy wrap using an average-width approximation for Helvetica."""
    per_char = size * 0.50
    limit = max(8, int(width / per_char))
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) <= limit:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def build_pdf(markdown: str, title: str = "", subtitle: str = "") -> bytes:
    """A real PDF, written by hand.

    Core fonts only, so the text layer is Latin-1. Anything outside that — Hindi,
    Bengali, Tamil — needs an embedded font, which is a much larger job than this
    file should be; for those, export Word and print from there. The interface
    says so at the point of choice rather than quietly producing question marks.
    """
    blocks = parse_markdown(markdown)
    content_width = _PAGE_W - 2 * _MARGIN
    pages: list[list[str]] = []
    stream: list[str] = []
    y = _PAGE_H - _MARGIN

    def new_page() -> None:
        nonlocal stream, y
        if stream:
            pages.append(stream)
        stream = []
        y = _PAGE_H - _MARGIN

    def write(text: str, size: float, bold: bool, gap: float, indent: float = 0.0) -> None:
        nonlocal y
        font = "F2" if bold else "F1"
        for line in _wrap(text, size, content_width - indent):
            if y < _MARGIN + 40:
                new_page()
            stream.append(
                f"BT /{font} {size:.1f} Tf {_MARGIN + indent:.1f} {y:.1f} Td "
                f"({_pdf_escape(line)}) Tj ET"
            )
            y -= size + 3
        y -= gap

    if title:
        write(title, 20, True, 6)
    if subtitle:
        stream.append("0.36 0.42 0.39 rg")
        write(subtitle, 9, False, 14)
        stream.append("0 0 0 rg")

    sizes = {"h1": 15.0, "h2": 13.0, "h3": 11.5, "h4": 10.5}
    for block in blocks:
        if block.kind in sizes:
            y -= 6
            write(strip_markdown(block.text), sizes[block.kind], True, 4)
        elif block.kind == "bullet":
            write("\u2022 " + strip_markdown(block.text), 10, False, 2, 12 + block.level * 12)
        elif block.kind == "number":
            write("- " + strip_markdown(block.text), 10, False, 2, 12 + block.level * 12)
        elif block.kind == "quote":
            write(strip_markdown(block.text), 10, False, 4, 18)
        elif block.kind == "code":
            for line in block.text.split("\n"):
                write(line, 9, False, 0, 14)
            y -= 6
        elif block.kind == "rule":
            if y < _MARGIN + 40:
                new_page()
            stream.append(
                f"0.85 0.87 0.85 RG 0.7 w {_MARGIN} {y:.1f} m {_PAGE_W - _MARGIN} {y:.1f} l S"
            )
            y -= 12
        elif block.kind == "pagebreak":
            new_page()
        elif block.kind == "table" and block.rows:
            for row_index, row in enumerate(block.rows):
                write(
                    "   ".join(strip_markdown(cell) for cell in row),
                    9.5, row_index == 0, 1,
                )
            y -= 8
        else:
            write(strip_markdown(block.text), 10, False, 6)

    if stream:
        pages.append(stream)
    if not pages:
        pages = [[f"BT /F1 10 Tf {_MARGIN} {_PAGE_H - _MARGIN} Td () Tj ET"]]

    return _assemble_pdf(pages, title)


def _assemble_pdf(pages: list[list[str]], title: str) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    pages_id = len(objects) + 1 + 2 * len(pages)
    page_ids: list[int] = []
    for stream in pages:
        data = ("\n".join(stream)).encode("latin-1", "replace")
        content_id = add(b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n" + data + b"\nendstream")
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_PAGE_W:.2f} {_PAGE_H:.2f}] "
            f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        ))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    info_id = add(
        f"<< /Title ({_pdf_escape(title or 'Float document')}) /Producer (Float {config.VERSION}) >>".encode()
    )
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


# ---------------------------------------------------------------------------
# Spreadsheet
# ---------------------------------------------------------------------------
_XLSX_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_XLSX_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1E3A32"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>
</styleSheet>"""


def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def build_xlsx(rows: list[list[Any]], sheet_name: str = "Sheet1", header: bool = True) -> bytes:
    xml_rows: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c, value in enumerate(row):
            ref = f"{_column(c)}{r}"
            style = ' s="1"' if header and r == 1 else ""
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                text = html.escape(str(value if value is not None else ""))
                cells.append(f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')

    width = max((len(r) for r in rows), default=1)
    cols = "".join(
        f'<col min="{i+1}" max="{i+1}" width="{min(42, max(11, max((len(str(r[i])) for r in rows if i < len(r)), default=10) + 3))}" customWidth="1"/>'
        for i in range(width)
    )
    freeze = (
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        if header else ""
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'{freeze}<cols>{cols}</cols><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{html.escape(sheet_name[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _XLSX_TYPES)
        archive.writestr("_rels/.rels", _XLSX_RELS)
        archive.writestr("xl/_rels/workbook.xml.rels", _XLSX_WB_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/styles.xml", _XLSX_STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def build_csv(rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")   # BOM so Excel opens Hindi correctly


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def build_html(markdown: str, title: str = "", subtitle: str = "") -> bytes:
    body: list[str] = []
    list_open: str | None = None

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            body.append(f"</{list_open}>")
            list_open = None

    def runs(text: str) -> str:
        out = []
        for fragment, styles in inline_runs(text):
            escaped = html.escape(fragment)
            if "bold" in styles:
                escaped = f"<strong>{escaped}</strong>"
            elif "italic" in styles:
                escaped = f"<em>{escaped}</em>"
            elif "code" in styles:
                escaped = f"<code>{escaped}</code>"
            out.append(escaped)
        return "".join(out)

    for block in parse_markdown(markdown):
        if block.kind == "bullet":
            if list_open != "ul":
                close_list()
                body.append("<ul>")
                list_open = "ul"
            body.append(f"<li>{runs(block.text)}</li>")
            continue
        if block.kind == "number":
            if list_open != "ol":
                close_list()
                body.append("<ol>")
                list_open = "ol"
            body.append(f"<li>{runs(block.text)}</li>")
            continue
        close_list()
        if block.kind.startswith("h"):
            body.append(f"<{block.kind}>{runs(block.text)}</{block.kind}>")
        elif block.kind == "quote":
            body.append(f"<blockquote>{runs(block.text)}</blockquote>")
        elif block.kind == "code":
            body.append(f"<pre><code>{html.escape(block.text)}</code></pre>")
        elif block.kind == "rule":
            body.append("<hr>")
        elif block.kind == "pagebreak":
            body.append('<div class="pagebreak"></div>')
        elif block.kind == "table" and block.rows:
            cells = "".join(f"<th>{runs(c)}</th>" for c in block.rows[0])
            rows = "".join(
                "<tr>" + "".join(f"<td>{runs(c)}</td>" for c in row) + "</tr>"
                for row in block.rows[1:]
            )
            body.append(f"<table><thead><tr>{cells}</tr></thead><tbody>{rows}</tbody></table>")
        else:
            body.append(f"<p>{runs(block.text)}</p>")
    close_list()

    heading = f"<h1 class='doc-title'>{html.escape(title)}</h1>" if title else ""
    sub = f"<p class='doc-sub'>{html.escape(subtitle)}</p>" if subtitle else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title or 'Float document')}</title>
<style>
  :root {{ --ink:#17211C; --board:#1E3A32; --soft:#5C6B63; --line:#D8DED9; --paper:#FBF9F4; }}
  body {{ font:16px/1.65 "Segoe UI",system-ui,sans-serif; color:var(--ink); background:var(--paper);
         max-width:44rem; margin:0 auto; padding:3rem 1.5rem 5rem; }}
  .doc-title {{ font-family:"Times New Roman",Times,serif; font-size:2.2rem; line-height:1.15;
                margin:0 0 .25rem; color:var(--board); letter-spacing:-.01em; }}
  .doc-sub {{ color:var(--soft); font-size:.85rem; margin:0 0 2.5rem; }}
  h1,h2,h3,h4 {{ color:var(--board); line-height:1.25; margin:2rem 0 .5rem; }}
  h1 {{ font-size:1.6rem }} h2 {{ font-size:1.3rem }} h3 {{ font-size:1.1rem }}
  blockquote {{ border-left:3px solid var(--line); margin:1rem 0; padding:.2rem 0 .2rem 1rem; color:var(--soft) }}
  table {{ border-collapse:collapse; width:100%; margin:1.25rem 0; font-size:.94rem }}
  th,td {{ border:1px solid var(--line); padding:.5rem .65rem; text-align:left; vertical-align:top }}
  th {{ background:#EDF2EE }}
  pre {{ background:#F2F4F1; padding:.9rem 1rem; overflow-x:auto; border-radius:6px; font-size:.85rem }}
  code {{ font-family:ui-monospace,Consolas,monospace; font-size:.9em }}
  hr {{ border:0; border-top:1px solid var(--line); margin:2rem 0 }}
  .pagebreak {{ page-break-after:always }}
  @media print {{ body {{ background:#fff; padding:0 }} }}
</style></head><body>{heading}{sub}{''.join(body)}</body></html>"""
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
def render(kind: str, content: Any, title: str = "", subtitle: str = "") -> bytes:
    if kind == "docx":
        return build_docx(str(content), title, subtitle)
    if kind == "pdf":
        return build_pdf(str(content), title, subtitle)
    if kind == "html":
        return build_html(str(content), title, subtitle)
    if kind == "xlsx":
        return build_xlsx(_as_rows(content))
    if kind == "csv":
        return build_csv(_as_rows(content))
    if kind == "md":
        header = f"# {title}\n\n*{subtitle}*\n\n" if title else ""
        return (header + str(content)).encode("utf-8")
    return strip_markdown(str(content)).encode("utf-8")


def _as_rows(content: Any) -> list[list[Any]]:
    if isinstance(content, list) and content and isinstance(content[0], list):
        return content
    if isinstance(content, list) and content and isinstance(content[0], dict):
        keys = list(content[0].keys())
        return [keys] + [[row.get(k, "") for k in keys] for row in content]
    # Fall back to reading a Markdown table out of the text.
    for block in parse_markdown(str(content)):
        if block.kind == "table" and block.rows:
            return [[strip_markdown(cell) for cell in row] for row in block.rows]
    return [[line] for line in str(content).split("\n")]


def store(user_id: int, conv_id: int | None, name: str, kind: str, data: bytes) -> dict[str, Any]:
    """Write into Float's own file area and register it."""
    config.ensure_dirs()
    folder = Path(config.FILES_DIR) / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    filename = safe_name(name, kind)
    target = folder / f"{int(time.time() * 1000)}-{filename}"
    target.write_bytes(data)
    rel = str(target.relative_to(config.FILES_DIR))
    artifact_id = db.add_artifact(user_id, conv_id, filename, kind, rel, len(data))
    return {
        "id": artifact_id,
        "name": filename,
        "kind": kind,
        "size": len(data),
        "label": KINDS.get(kind, (kind.upper(), ""))[0],
    }


def artifact_bytes(artifact: dict[str, Any]) -> bytes:
    path = Path(config.FILES_DIR) / artifact["rel_path"]
    resolved = path.resolve()
    if not str(resolved).startswith(str(Path(config.FILES_DIR).resolve())):
        raise PermissionError("Refused: path escapes the file area.")
    return resolved.read_bytes()


def desktop_dir() -> Path:
    """Where 'Save to Desktop' puts things, on any of the three platforms."""
    home = Path.home()
    candidates = [home / "Desktop", home / "OneDrive" / "Desktop", home / "Área de Trabalho"]
    if platform.system() == "Windows":
        try:
            import winreg  # type: ignore

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            value, _ = winreg.QueryValueEx(key, "Desktop")
            candidates.insert(0, Path(os.path.expandvars(value)))
        except Exception:
            pass
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return home


def save_to_desktop(artifact: dict[str, Any], folder: str = "Float") -> Path:
    target_dir = desktop_dir() / folder if folder else desktop_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / artifact["name"]
    counter = 2
    while target.exists():
        target = target_dir / f"{Path(artifact['name']).stem} ({counter}){Path(artifact['name']).suffix}"
        counter += 1
    target.write_bytes(artifact_bytes(artifact))
    return target


def reveal(path: Path) -> bool:
    """Open the containing folder, so 'saved' is something the teacher can see."""
    try:
        system = platform.system()
        if system == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except Exception:
        return False
