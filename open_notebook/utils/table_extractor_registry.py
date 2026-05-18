"""
Table Extractor Registry — Phase 1A
====================================
Dispatches to a per-file-type extractor that returns a list of ExtractedTable
objects.  All extractors are wrapped in try/except so extraction failures
never propagate to the ingestion graph.

Supported in Phase 1A:
  .csv          → stdlib csv.DictReader     (no pandas)
  .xlsx / .xls  → openpyxl                  (already in pyproject.toml transitive deps)
  .docx         → python-docx XML traversal (already present)
  .pdf          → fitz/PyMuPDF              (already used in pdf_table_preserver.py)

HTML deferred to Phase 2 (content-core already converts <table> → Markdown).
"""

import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel, Field

# ── Row limit ────────────────────────────────────────────────────────────────
TABLE_MAX_ROWS: int = int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_ROWS", "5000"))


# ── ExtractedTable model ─────────────────────────────────────────────────────

class ExtractedTable(BaseModel):
    """Structured representation of a single table extracted from a source file."""

    table_id: str
    source_id: str
    page_number: Optional[int] = None     # 1-based for PDF; None for all others
    sheet_name: Optional[str] = None      # XLSX sheet name; None for all others
    column_headers: List[str] = Field(default_factory=list)
    row_data: List[Dict[str, Any]] = Field(default_factory=list)
    markdown_repr: str = ""
    row_count: int = 0
    col_count: int = 0
    truncated: bool = False


# ── Markdown generation helper ────────────────────────────────────────────────

def _to_markdown(headers: List[str], rows: List[Dict[str, Any]]) -> str:
    """Generate a GFM Markdown table from headers and row dicts."""
    if not headers:
        return ""
    sep = "|" + "|".join("---" for _ in headers) + "|"
    header_row = "| " + " | ".join(str(h) for h in headers) + " |"
    data_rows = [
        "| " + " | ".join(str(row.get(h, "")) for h in headers) + " |"
        for row in rows
    ]
    return "\n".join([header_row, sep] + data_rows)


# ── CSV extractor ─────────────────────────────────────────────────────────────

def _extract_csv(file_path: str, source_id: str, table_index: int) -> List[ExtractedTable]:
    """Extract a single table from a CSV file using stdlib csv.DictReader."""
    rows: List[Dict[str, Any]] = []
    truncated = False

    with open(file_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        if not headers:
            logger.warning(f"CSV extractor: no headers found in {file_path}")
            return []

        for row in reader:
            if len(rows) >= TABLE_MAX_ROWS:
                truncated = True
                logger.warning(
                    f"CSV extractor: row limit {TABLE_MAX_ROWS} reached for {file_path}; truncating"
                )
                break
            # Coerce all values to str and normalise None
            rows.append({h: str(row.get(h) or "") for h in headers})

    table_id = f"{source_id}_table_{table_index}"
    return [
        ExtractedTable(
            table_id=table_id,
            source_id=source_id,
            page_number=None,
            sheet_name=None,
            column_headers=headers,
            row_data=rows,
            markdown_repr=_to_markdown(headers, rows),
            row_count=len(rows),
            col_count=len(headers),
            truncated=truncated,
        )
    ]


# ── XLSX extractor ────────────────────────────────────────────────────────────

def _extract_xlsx(file_path: str, source_id: str, table_index_start: int) -> List[ExtractedTable]:
    """Extract one ExtractedTable per sheet from an XLSX file using openpyxl."""
    import openpyxl  # type: ignore

    tables: List[ExtractedTable] = []
    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    try:
        for sheet_offset, ws in enumerate(wb.worksheets):
            sheet_name = ws.title or f"Sheet{sheet_offset + 1}"
            all_rows = list(ws.iter_rows(values_only=True))
            if not all_rows:
                continue

            # First row → headers
            raw_headers = [str(cell) if cell is not None else f"Col{i}" for i, cell in enumerate(all_rows[0])]
            headers = raw_headers

            truncated = False
            rows: List[Dict[str, Any]] = []
            for raw_row in all_rows[1:]:
                if len(rows) >= TABLE_MAX_ROWS:
                    truncated = True
                    logger.warning(
                        f"XLSX extractor: row limit {TABLE_MAX_ROWS} reached in sheet '{sheet_name}'; truncating"
                    )
                    break
                row_dict = {headers[i]: str(cell) if cell is not None else "" for i, cell in enumerate(raw_row)}
                rows.append(row_dict)

            if not rows:
                continue  # Skip sheets with only a header row and no data

            table_id = f"{source_id}_table_{table_index_start + sheet_offset}"
            tables.append(
                ExtractedTable(
                    table_id=table_id,
                    source_id=source_id,
                    page_number=None,
                    sheet_name=sheet_name,
                    column_headers=headers,
                    row_data=rows,
                    markdown_repr=_to_markdown(headers, rows),
                    row_count=len(rows),
                    col_count=len(headers),
                    truncated=truncated,
                )
            )
    finally:
        wb.close()
    return tables


# ── PDF extractor ─────────────────────────────────────────────────────────────

def _extract_pdf(file_path: str, source_id: str, table_index_start: int) -> List[ExtractedTable]:
    """
    Extract structured tables from a PDF using fitz/PyMuPDF.

    Reuses the same page.find_tables() + table.extract() pattern already
    established in pdf_table_preserver.py.  Extraction failures on individual
    pages are caught and logged — they never raise to the caller.
    """
    import fitz  # type: ignore

    tables: List[ExtractedTable] = []
    global_idx = table_index_start

    doc = fitz.open(file_path)
    try:
        for page_num, page in enumerate(doc):
            try:
                detected = page.find_tables()
                if not detected:
                    continue

                for table_obj in detected:
                    raw_data = table_obj.extract()  # list[list[str|None]]
                    if not raw_data or len(raw_data) < 2:
                        # Need at least one header row + one data row
                        continue

                    # First row is headers
                    headers = [str(cell).strip() if cell else f"Col{i}" for i, cell in enumerate(raw_data[0])]
                    rows: List[Dict[str, Any]] = []
                    truncated = False

                    for raw_row in raw_data[1:]:
                        if len(rows) >= TABLE_MAX_ROWS:
                            truncated = True
                            logger.warning(
                                f"PDF extractor: row limit {TABLE_MAX_ROWS} reached on page {page_num + 1}; truncating"
                            )
                            break
                        row_dict = {headers[i]: str(cell).strip() if cell else "" for i, cell in enumerate(raw_row)}
                        rows.append(row_dict)

                    if not rows:
                        continue

                    table_id = f"{source_id}_table_{global_idx}"
                    global_idx += 1
                    tables.append(
                        ExtractedTable(
                            table_id=table_id,
                            source_id=source_id,
                            page_number=page_num + 1,  # 1-based
                            sheet_name=None,
                            column_headers=headers,
                            row_data=rows,
                            markdown_repr=_to_markdown(headers, rows),
                            row_count=len(rows),
                            col_count=len(headers),
                            truncated=truncated,
                        )
                    )
            except Exception as exc:
                logger.warning(
                    f"PDF extractor: table extraction failed on page {page_num + 1} of '{file_path}': {exc}"
                )
                # Continue to next page — never raise
    finally:
        doc.close()

    return tables


# ── DOCX extractor ─────────────────────────────────────────────────────────────

def _extract_docx(file_path: str, source_id: str, table_index_start: int) -> List[ExtractedTable]:
    """
    Extract structured tables from a DOCX file using python-docx XML traversal.

    Extends the approach in docx_table_extractor.py: iterates doc.element.body
    and pulls every w:tbl element into an ExtractedTable.  Preserves the
    existing extract_docx_with_tables() function unchanged (it is still used
    in source.py for prose+table full_text extraction).
    """
    import docx as python_docx  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    tables: List[ExtractedTable] = []
    doc = python_docx.Document(file_path)

    for table_offset, tbl_elem in enumerate(
        doc.element.body.iter(qn("w:tbl"))
    ):
        # Reconstruct a python-docx Table object from the XML element
        from docx.table import Table as DocxTable  # type: ignore
        tbl = DocxTable(tbl_elem, doc)

        # Read rows
        all_rows_text: List[List[str]] = []
        for row in tbl.rows:
            all_rows_text.append([cell.text.strip() for cell in row.cells])

        if len(all_rows_text) < 2:
            continue  # Skip tables with only headers or fewer

        headers = all_rows_text[0]
        if not any(h.strip() for h in headers):
            continue  # Skip tables with blank header rows

        truncated = False
        rows: List[Dict[str, Any]] = []
        for raw_row in all_rows_text[1:]:
            if len(rows) >= TABLE_MAX_ROWS:
                truncated = True
                logger.warning(
                    f"DOCX extractor: row limit {TABLE_MAX_ROWS} reached in table {table_offset} of '{file_path}'; truncating"
                )
                break
            # Pad short rows to match header count
            padded = raw_row + [""] * max(0, len(headers) - len(raw_row))
            rows.append({headers[i]: padded[i] for i in range(len(headers))})

        if not rows:
            continue

        table_id = f"{source_id}_table_{table_index_start + table_offset}"
        tables.append(
            ExtractedTable(
                table_id=table_id,
                source_id=source_id,
                page_number=None,
                sheet_name=None,
                column_headers=headers,
                row_data=rows,
                markdown_repr=_to_markdown(headers, rows),
                row_count=len(rows),
                col_count=len(headers),
                truncated=truncated,
            )
        )

    return tables


# ── Dispatcher ────────────────────────────────────────────────────────────────

def extract_tables_from_source(file_path: str, source_id: str) -> List[ExtractedTable]:
    """
    Dispatch to the correct per-file-type extractor based on file extension.

    Returns an empty list for unknown types or when extraction fails entirely.
    Per-file extraction errors are caught and logged; they never propagate.

    Args:
        file_path:  Absolute path to the source file.
        source_id:  The source record ID (used to build deterministic table_ids).

    Returns:
        List of ExtractedTable objects (may be empty).
    """
    if not file_path:
        return []

    ext = Path(file_path).suffix.lower()
    logger.debug(f"Table extractor: dispatching for extension '{ext}' on '{file_path}'")

    try:
        if ext == ".csv":
            return _extract_csv(file_path, source_id, 0)
        elif ext in (".xlsx", ".xls"):
            return _extract_xlsx(file_path, source_id, 0)
        elif ext == ".pdf":
            return _extract_pdf(file_path, source_id, 0)
        elif ext == ".docx":
            return _extract_docx(file_path, source_id, 0)
        else:
            logger.debug(
                f"Table extractor: no extractor registered for extension '{ext}'; returning empty list"
            )
            return []
    except Exception as exc:
        logger.warning(
            f"Table extractor: extraction failed for '{file_path}' ({ext}): {exc}. "
            "Returning empty list — ingestion continues."
        )
        return []
