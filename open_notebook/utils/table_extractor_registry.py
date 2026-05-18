"""
Table Extractor Registry — Phase 2A-1
=======================================
Dispatches to a per-file-type extractor that returns a list of ExtractedTable
objects.  All extractors are wrapped in try/except so extraction failures
never propagate to the ingestion graph.

Supported:
  .csv          → stdlib csv.DictReader     (no pandas)
  .xlsx         → openpyxl                  (already in pyproject.toml transitive deps)
  .xls          → user-visible warning; xlrd not added (see .xls decision gate)
  .docx         → python-docx XML traversal (already present)
  .pdf          → fitz/PyMuPDF              (already used in pdf_table_preserver.py)
  .html / .htm  → stdlib html.parser        (Phase 2A new — no new dependency)

Phase 2 changes vs Phase 1:
  • TABLE_MAX_COLS: column-count cap enforced uniformly in _apply_col_cap().
  • ExtractedTable gains optional_headers: Optional[List[str]] for XLSX dedup.
  • XLSX extractor deduplicates column headers (_2/_3 suffix), stores originals.
  • HTML extractor added for .html/.htm files.
  • .xls files return [] with a user-visible warning instead of being routed
    to the XLSX extractor (xlrd is not added; openpyxl cannot read .xls).
  • _apply_col_cap() is the single enforcement point for both row and col caps
    after each per-type extractor returns.
"""

import csv
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel, Field

# ── Limits (read once at module load; overridden in tests via monkeypatch) ─────
TABLE_MAX_ROWS: int = int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_ROWS", "5000"))
TABLE_MAX_COLS: int = int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_COLS", "100"))


# ── ExtractedTable model ──────────────────────────────────────────────────────

class ExtractedTable(BaseModel):
    """Structured representation of a single table extracted from a source file."""

    table_id: str
    source_id: str
    page_number: Optional[int] = None     # 1-based for PDF; None for all others
    sheet_name: Optional[str] = None      # XLSX sheet name; None for all others
    column_headers: List[str] = Field(default_factory=list)
    # original_headers stores pre-deduplication headers for XLSX sheets.
    # None for non-XLSX extractors or when no deduplication was needed.
    original_headers: Optional[List[str]] = None
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


# ── Column-cap enforcement (Phase 2 — uniform across all extractors) ──────────

def _apply_col_cap(
    headers: List[str],
    rows: List[Dict[str, Any]],
    already_truncated: bool,
    context: str = "",
) -> tuple[List[str], List[Dict[str, Any]], bool]:
    """
    If the table has more columns than TABLE_MAX_COLS, drop excess columns,
    set truncated=True, and log a warning.

    Returns (capped_headers, capped_rows, truncated_flag).
    """
    if len(headers) <= TABLE_MAX_COLS:
        return headers, rows, already_truncated

    logger.warning(
        f"Table extractor{' [' + context + ']' if context else ''}: "
        f"column limit {TABLE_MAX_COLS} reached "
        f"({len(headers)} columns); dropping excess columns"
    )
    capped_headers = headers[:TABLE_MAX_COLS]
    capped_rows = [
        {h: row.get(h, "") for h in capped_headers}
        for row in rows
    ]
    return capped_headers, capped_rows, True


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

    # Apply column cap (Phase 2)
    headers, rows, truncated = _apply_col_cap(headers, rows, truncated, context=file_path)

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


# ── XLSX duplicate-header deduplication helper ────────────────────────────────

def _deduplicate_headers(raw_headers: List[str]) -> tuple[List[str], Optional[List[str]]]:
    """
    Detect duplicate column headers and rename them with _2, _3 ... suffixes
    (matching the pandas convention).

    Returns (deduped_headers, original_headers_or_None).
    original_headers is None when no duplicates were found (avoids storing
    redundant data for the common case).
    """
    seen: Dict[str, int] = {}
    result: List[str] = []
    has_duplicates = False

    for h in raw_headers:
        if h in seen:
            has_duplicates = True
            seen[h] += 1
            result.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 1
            result.append(h)

    if has_duplicates:
        logger.warning(
            f"XLSX extractor: duplicate column headers detected — "
            f"renamed with _2/_3 suffixes. Original: {raw_headers}"
        )
        return result, raw_headers

    return result, None  # No duplicates → original_headers not needed


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

            # First row → headers; fill None cells with positional placeholder
            raw_headers = [
                str(cell) if cell is not None else f"Col{i}"
                for i, cell in enumerate(all_rows[0])
            ]

            # Phase 2: deduplicate headers, store originals if needed
            headers, original_headers = _deduplicate_headers(raw_headers)

            truncated = False
            rows: List[Dict[str, Any]] = []
            for raw_row in all_rows[1:]:
                if len(rows) >= TABLE_MAX_ROWS:
                    truncated = True
                    logger.warning(
                        f"XLSX extractor: row limit {TABLE_MAX_ROWS} reached in "
                        f"sheet '{sheet_name}'; truncating"
                    )
                    break
                # Use deduplicated headers as dict keys
                row_dict = {
                    headers[i]: str(cell) if cell is not None else ""
                    for i, cell in enumerate(raw_row)
                    if i < len(headers)
                }
                rows.append(row_dict)

            if not rows:
                continue  # Skip sheets with only a header row and no data

            # Apply column cap (Phase 2)
            headers, rows, truncated = _apply_col_cap(
                headers, rows, truncated,
                context=f"{file_path}!{sheet_name}"
            )

            table_id = f"{source_id}_table_{table_index_start + sheet_offset}"
            tables.append(
                ExtractedTable(
                    table_id=table_id,
                    source_id=source_id,
                    page_number=None,
                    sheet_name=sheet_name,
                    column_headers=headers,
                    original_headers=original_headers,
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


# ── .xls decision gate (Phase 2) ─────────────────────────────────────────────

def _extract_xls(file_path: str, source_id: str, table_index_start: int) -> List[ExtractedTable]:
    """
    .xls files are the legacy Excel binary format.  openpyxl cannot read them
    and xlrd (the only viable reader) is unmaintained and has had security
    vulnerabilities, so we deliberately do NOT add it as a dependency.

    We return an empty list and surface a user-visible warning so the operator
    or user knows to convert the file.
    """
    logger.warning(
        f"XLS files are not supported. Please convert '{Path(file_path).name}' "
        f"to XLSX format. Returning empty table list — ingestion continues."
    )
    return []


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
                    headers = [
                        str(cell).strip() if cell else f"Col{i}"
                        for i, cell in enumerate(raw_data[0])
                    ]
                    rows: List[Dict[str, Any]] = []
                    truncated = False

                    for raw_row in raw_data[1:]:
                        if len(rows) >= TABLE_MAX_ROWS:
                            truncated = True
                            logger.warning(
                                f"PDF extractor: row limit {TABLE_MAX_ROWS} reached "
                                f"on page {page_num + 1}; truncating"
                            )
                            break
                        row_dict = {
                            headers[i]: str(cell).strip() if cell else ""
                            for i, cell in enumerate(raw_row)
                            if i < len(headers)
                        }
                        rows.append(row_dict)

                    if not rows:
                        continue

                    # Apply column cap (Phase 2)
                    headers, rows, truncated = _apply_col_cap(
                        headers, rows, truncated,
                        context=f"{file_path} page {page_num + 1}"
                    )

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
                    f"PDF extractor: table extraction failed on page {page_num + 1} "
                    f"of '{file_path}': {exc}"
                )
                # Continue to next page — never raise
    finally:
        doc.close()

    return tables


# ── DOCX extractor ────────────────────────────────────────────────────────────

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
                    f"DOCX extractor: row limit {TABLE_MAX_ROWS} reached in "
                    f"table {table_offset} of '{file_path}'; truncating"
                )
                break
            # Pad short rows to match header count
            padded = raw_row + [""] * max(0, len(headers) - len(raw_row))
            rows.append({headers[i]: padded[i] for i in range(len(headers))})

        if not rows:
            continue

        # Apply column cap (Phase 2)
        headers, rows, truncated = _apply_col_cap(
            headers, rows, truncated,
            context=f"{file_path} table {table_offset}"
        )

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


# ── HTML extractor (Phase 2A) ─────────────────────────────────────────────────

class _HTMLTableParser(HTMLParser):
    """
    Minimal stdlib html.parser-based table extractor.

    Parses <table> elements into a list of row lists.  Handles nested tables
    by tracking depth: inner <table> elements are flattened (their cells are
    treated as plain text).  Only top-level tables are emitted as separate
    ExtractedTable records.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth: int = 0                   # nesting depth of <table> elements
        self._current_row: List[str] = []
        self._current_cell: List[str] = []
        self._current_table: List[List[str]] = []
        self.tables: List[List[List[str]]] = []  # completed top-level tables
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                # Start a fresh top-level table
                self._current_table = []
        elif tag in ("tr",) and self._depth == 1:
            self._current_row = []
        elif tag in ("td", "th") and self._depth == 1:
            self._current_cell = []
            self._in_cell = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._depth == 1 and self._current_table:
                self.tables.append(self._current_table)
                self._current_table = []
            self._depth = max(0, self._depth - 1)
        elif tag == "tr" and self._depth == 1:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = []
        elif tag in ("td", "th") and self._depth == 1:
            cell_text = " ".join(self._current_cell).strip()
            self._current_row.append(cell_text)
            self._current_cell = []
            self._in_cell = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            stripped = data.strip()
            if stripped:
                self._current_cell.append(stripped)


def _extract_html(file_path: str, source_id: str, table_index_start: int) -> List[ExtractedTable]:
    """
    Extract tables from an HTML file using stdlib html.parser.

    • page_number and sheet_name are both None (HTML has no page/sheet concept).
    • Nested tables are flattened — inner cells appear as text in the outer cell.
    • Malformed HTML returns [] with a warning instead of raising.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        logger.warning(f"HTML extractor: could not read '{file_path}': {exc}")
        return []

    parser = _HTMLTableParser()
    try:
        parser.feed(content)
    except Exception as exc:
        logger.warning(f"HTML extractor: parsing failed for '{file_path}': {exc}")
        return []

    if not parser.tables:
        logger.debug(f"HTML extractor: no <table> elements found in '{file_path}'")
        return []

    tables: List[ExtractedTable] = []
    for table_offset, raw_table in enumerate(parser.tables):
        if len(raw_table) < 2:
            # Need header row + at least one data row
            continue

        headers = [str(h) if h else f"Col{i}" for i, h in enumerate(raw_table[0])]
        if not any(h.strip() for h in headers):
            continue  # Skip tables with all-blank header rows

        truncated = False
        rows: List[Dict[str, Any]] = []
        for raw_row in raw_table[1:]:
            if len(rows) >= TABLE_MAX_ROWS:
                truncated = True
                logger.warning(
                    f"HTML extractor: row limit {TABLE_MAX_ROWS} reached in "
                    f"table {table_offset} of '{file_path}'; truncating"
                )
                break
            # Pad short rows to match header count
            padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
            rows.append({headers[i]: padded[i] for i in range(len(headers))})

        if not rows:
            continue

        # Apply column cap (Phase 2)
        headers, rows, truncated = _apply_col_cap(
            headers, rows, truncated,
            context=f"{file_path} table {table_offset}"
        )

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
        elif ext == ".xlsx":
            return _extract_xlsx(file_path, source_id, 0)
        elif ext == ".xls":
            # .xls is the legacy Excel binary format; openpyxl cannot read it.
            # xlrd is deliberately not added — see _extract_xls() for rationale.
            return _extract_xls(file_path, source_id, 0)
        elif ext == ".pdf":
            return _extract_pdf(file_path, source_id, 0)
        elif ext == ".docx":
            return _extract_docx(file_path, source_id, 0)
        elif ext in (".html", ".htm"):
            return _extract_html(file_path, source_id, 0)
        else:
            logger.debug(
                f"Table extractor: no extractor registered for extension '{ext}'; "
                "returning empty list"
            )
            return []
    except Exception as exc:
        logger.warning(
            f"Table extractor: extraction failed for '{file_path}' ({ext}): {exc}. "
            "Returning empty list — ingestion continues."
        )
        return []
