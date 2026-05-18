"""
Custom PDF extractor that preserves Markdown table syntax through the
clean_pdf_text() sanitisation pass.

Root cause of the bug:
  content_core/processors/pdf.py appends Markdown table rows (| col | col |)
  to page_text, then passes the *entire* string through clean_pdf_text().
  clean_pdf_text() has a step `re.sub(r"[ \t]+", " ", text)` and removes
  spaces before newlines, which collapses "| col |" into plain text.

Fix strategy (D3 from design.md):
  1. Run page extraction with content_core's table detection.
  2. Use sentinel markers (<<<TABLE_START>>> / <<<TABLE_END>>>) to wrap
     each Markdown table block so we can find them after extraction.
  3. Split on those markers, apply clean_pdf_text() only to text segments,
     then reassemble.
"""

import re

import fitz  # type: ignore
from loguru import logger

from content_core.processors.pdf import clean_pdf_text, convert_table_to_markdown

# Unique sentinels that are extremely unlikely to appear in real text
_TABLE_START = "<<<TABLE_START>>>"
_TABLE_END = "<<<TABLE_END>>>"

# Regex to locate sentinel-wrapped blocks (including their markers)
_TABLE_BLOCK_RE = re.compile(
    re.escape(_TABLE_START) + r"(.*?)" + re.escape(_TABLE_END),
    re.DOTALL,
)


def _preserve_tables_in_clean(text: str) -> str:
    """
    Apply clean_pdf_text() only to non-table portions of *text*.

    Tables are wrapped in <<<TABLE_START>>> / <<<TABLE_END>>> markers.
    We split the text on these markers, clean the text-only segments,
    and reassemble with the original Markdown table blocks untouched.

    Args:
        text: Raw combined page text containing sentinel-wrapped table blocks.

    Returns:
        Cleaned text with Markdown tables preserved.
    """
    # Split the text into alternating [text, table, text, table, …] segments.
    # re.split with a capturing group keeps the captured content in the list.
    parts = re.split(
        f"({re.escape(_TABLE_START)}.*?{re.escape(_TABLE_END)})",
        text,
        flags=re.DOTALL,
    )

    result: list[str] = []
    for part in parts:
        if part.startswith(_TABLE_START) and part.endswith(_TABLE_END):
            # Strip sentinels and keep the Markdown table verbatim
            table_content = part[len(_TABLE_START) : -len(_TABLE_END)]
            result.append(table_content)
        else:
            # Only clean the regular text portion
            result.append(clean_pdf_text(part))

    return "".join(result)


def extract_pdf_with_tables(file_path: str) -> str:
    """
    Extract text from a PDF file while preserving Markdown-formatted tables.

    Uses PyMuPDF (fitz) for text extraction and table detection, mirroring
    content_core's _extract_text_from_pdf() logic but wrapping table blocks
    in sentinels before the clean_pdf_text() pass.

    Args:
        file_path: Absolute path to the PDF file.

    Returns:
        Cleaned Markdown string with tables intact.

    Raises:
        Exception: Propagated to the caller so graphs/source.py can fall back.
    """
    doc = fitz.open(file_path)
    try:
        full_text_parts: list[str] = []
        logger.debug(f"PDF table preserver: processing {len(doc)} pages in '{file_path}'")

        extraction_flags = (
            fitz.TEXT_PRESERVE_LIGATURES
            | fitz.TEXT_PRESERVE_WHITESPACE
            | fitz.TEXT_PRESERVE_IMAGES
        )

        for page_num, page in enumerate(doc):
            page_text: str = page.get_text(flags=extraction_flags)

            # Detect and extract tables, wrapping them in sentinels
            try:
                tables = page.find_tables()
                if tables:
                    logger.debug(
                        f"PDF table preserver: found {len(tables)} table(s) on page {page_num + 1}"
                    )
                    for table_num, table in enumerate(tables):
                        table_data = table.extract()
                        has_content = (
                            table_data
                            and len(table_data) > 0
                            and any(
                                any(str(cell).strip() for cell in row if cell)
                                for row in table_data
                                if row
                            )
                        )
                        if has_content:
                            md_table = convert_table_to_markdown(table_data)
                            # Wrap with sentinels so clean_pdf_text skips this block
                            page_text += (
                                f"\n\n{_TABLE_START}\n\n"
                                f"*Table {table_num + 1} (page {page_num + 1})*\n\n"
                                f"{md_table}"
                                f"\n{_TABLE_END}\n"
                            )
            except Exception as exc:
                logger.debug(
                    f"PDF table preserver: table extraction failed on page {page_num + 1}: {exc}"
                )

            full_text_parts.append(page_text)

        combined = "".join(full_text_parts)
        return _preserve_tables_in_clean(combined)
    finally:
        doc.close()
