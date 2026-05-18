"""
Custom DOCX table extractor that preserves tables by traversing doc.element.body
in document order (paragraphs and tables interleaved correctly).

This module exists because content_core's office.py only iterates doc.paragraphs,
which silently drops all table content. We override the extraction for .docx files
in graphs/source.py before handing off to extract_content().
"""

from docx import Document  # type: ignore
from docx.oxml.ns import qn  # type: ignore
from loguru import logger


def _table_to_markdown(table) -> str:
    """
    Convert a python-docx Table object to GFM Markdown table syntax.

    Rules:
    - First row is treated as the header row.
    - Empty cells become empty strings.
    - Newlines inside cells are replaced with a space to keep the table valid.
    - If the table has no rows it is skipped (returns empty string).
    - Merged/spanned cells: we read cell.text which already flattens spans.
    """
    rows = table.rows
    if not rows:
        return ""

    def _cell_text(cell) -> str:
        text = cell.text.strip() if cell.text else ""
        # Replace newlines so the | … | format is not broken
        return text.replace("\n", " ").replace("\r", " ")

    # Build header
    header_cells = [_cell_text(c) for c in rows[0].cells]
    if not any(header_cells):
        # Completely empty header — skip entire table
        return ""

    lines = []
    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("| " + " | ".join(["---"] * len(header_cells)) + " |")

    # Data rows
    for row in rows[1:]:
        cells = [_cell_text(c) for c in row.cells]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def extract_docx_with_tables(file_path: str) -> str:
    """
    Extract content from a DOCX file while preserving tables.

    Traverses doc.element.body XML to honour the actual document order of
    paragraphs and tables (python-docx exposes them in separate lists, losing
    their relative ordering).

    Paragraph formatting logic is ported from content_core/processors/office.py
    so that headings, bold, italic, and list styles are retained.

    Args:
        file_path: Absolute path to the .docx file.

    Returns:
        Markdown-formatted string with all paragraphs and tables in order.

    Raises:
        Exception: Propagated to the caller so graphs/source.py can fall back.
    """
    doc = Document(file_path)
    content: list[str] = []

    # Build lookup maps so we can access python-docx objects by their XML element id
    para_map = {id(p._element): p for p in doc.paragraphs}
    table_map = {id(t._element): t for t in doc.tables}

    for child in doc.element.body:
        # ---- Paragraph ----
        if child.tag == qn("w:p"):
            paragraph = para_map.get(id(child))
            if paragraph is None:
                continue
            if not paragraph.text.strip():
                continue

            style = paragraph.style.name if paragraph.style else "Normal"
            text = paragraph.text.strip()

            # Indent
            p_format = paragraph.paragraph_format
            indent = p_format.left_indent or 0
            indent_level = 0
            if hasattr(indent, "pt"):
                indent_level = int(indent.pt / 72)
            indent_spaces = " " * (indent_level * 4)

            if "Heading" in style:
                level = style[-1] if style[-1].isdigit() else "1"
                heading_marks = "#" * int(level)
                content.append(f"\n{heading_marks} {text}\n")

            elif (
                paragraph.style
                and hasattr(paragraph.style, "name")
                and paragraph.style.name.startswith("List")
            ):
                # Numbered list
                if (
                    hasattr(paragraph._p, "pPr")
                    and paragraph._p.pPr is not None
                    and hasattr(paragraph._p.pPr, "numPr")
                    and paragraph._p.pPr.numPr is not None
                ):
                    try:
                        if (
                            hasattr(paragraph._p.pPr.numPr, "numId")
                            and paragraph._p.pPr.numPr.numId is not None
                            and hasattr(paragraph._p.pPr.numPr.numId, "val")
                        ):
                            number = paragraph._p.pPr.numPr.numId.val
                            content.append(f"{indent_spaces}{number}. {text}")
                        else:
                            content.append(f"{indent_spaces}1. {text}")
                    except Exception:
                        content.append(f"{indent_spaces}1. {text}")
                else:
                    content.append(f"{indent_spaces}* {text}")

            else:
                # Handle inline bold / italic formatting
                formatted_text: list[str] = []
                for run in paragraph.runs:
                    if run.bold:
                        formatted_text.append(f"**{run.text}**")
                    elif run.italic:
                        formatted_text.append(f"*{run.text}*")
                    else:
                        formatted_text.append(run.text)
                content.append(f"{indent_spaces}{''.join(formatted_text)}")

        # ---- Table ----
        elif child.tag == qn("w:tbl"):
            table = table_map.get(id(child))
            if table is None:
                continue
            md_table = _table_to_markdown(table)
            if md_table:
                content.append(f"\n{md_table}\n")

    return "\n\n".join(content)
