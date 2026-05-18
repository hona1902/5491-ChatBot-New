"""
table_lookup.py — Deterministic exact row lookup for structured sources.

Scope (Phase 1C):
  - Called only from source_chat.py where source_id is a known state field.
  - NOT called from ask.py (notebook-level QA) — that is a Phase 2 concern.
  - Restricted to CSV and XLSX sources; all other file types return None immediately.

Algorithm:
  1. Fetch all source_table records for the given source_id.
  2. Check source file extension — bail out early if not .csv / .xlsx.
  3. Tokenise the query into candidate lookup words (strip stop-words).
  4. Scan every row_data dict in every table record for case-insensitive cell
     value matches against any candidate word.
  5. Return a GFM Markdown table of matched rows (with their column headers),
     or None if nothing matched.

No pandas, no pdfplumber — stdlib only, plus the existing domain model.
"""

from __future__ import annotations

import re
from typing import Optional

from loguru import logger

from open_notebook.domain.notebook import Source, SourceTable

# ---------------------------------------------------------------------------
# Minimal English stop-word list — keeps token matching focused.
# Extend as needed; does not need to be exhaustive.
# ---------------------------------------------------------------------------
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "me",
        "my",
        "you",
        "your",
        "he",
        "she",
        "we",
        "they",
        "their",
        "not",
        "no",
        "s",
        "t",
    }
)

# File extensions treated as structured tabular sources eligible for exact lookup.
_STRUCTURED_EXTENSIONS: frozenset[str] = frozenset({".csv", ".xlsx"})


def _tokenize_query(query: str) -> list[str]:
    """Return non-stop-word tokens from *query* in lower-case."""
    raw_tokens = re.findall(r"\b[A-Za-z0-9_\-\.]+\b", query)
    return [t.lower() for t in raw_tokens if t.lower() not in _STOP_WORDS and len(t) > 1]


def _build_markdown_table(headers: list[str], rows: list[dict]) -> str:
    """Render *headers* and *rows* as a GFM Markdown table string."""
    # Header row
    header_row = "| " + " | ".join(str(h) for h in headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    data_rows = []
    for row in rows:
        cells = [str(row.get(h, "")) for h in headers]
        data_rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header_row, separator] + data_rows)


def _is_structured_source(source: Source) -> bool:
    """Return True if the source asset is a CSV or XLSX file."""
    if not source or not source.asset:
        return False
    file_path = source.asset.file_path or ""
    if not file_path:
        return False
    # Derive extension from the last dot-segment, lower-cased.
    # Handles paths like /data/uploads/report.xlsx and report.CSV
    ext = ""
    dot_pos = file_path.rfind(".")
    if dot_pos != -1:
        ext = file_path[dot_pos:].lower()
    return ext in _STRUCTURED_EXTENSIONS


async def table_exact_lookup(query: str, source_id: str) -> Optional[str]:
    """
    Deterministic row-level lookup against extracted table data for *source_id*.

    Returns a GFM Markdown table of all matching rows across all source_table
    records, or ``None`` if:
      - The source file is not .csv or .xlsx
      - No source_table records exist for this source
      - No cell value in any row matches any candidate token from *query*

    Raises:
        Nothing — all exceptions are caught and logged; ``None`` is returned
        on any error so source_chat falls through to vector-based context.
    """
    if not query or not source_id:
        return None

    try:
        # --- 1. Load the parent source to check file extension ---------------
        source = await Source.get(source_id)
        if source is None:
            logger.debug(f"table_exact_lookup: source {source_id} not found, skipping")
            return None

        # --- 2. Guard: only CSV / XLSX ----------------------------------------
        if not _is_structured_source(source):
            logger.debug(
                f"table_exact_lookup: source {source_id} is not CSV/XLSX, skipping"
            )
            return None

        # --- 3. Fetch source_table records ------------------------------------
        table_records: list[SourceTable] = await SourceTable.get_for_source(source_id)
        if not table_records:
            logger.debug(
                f"table_exact_lookup: no source_table records for {source_id}"
            )
            return None

        # --- 4. Tokenise query -----------------------------------------------
        tokens = _tokenize_query(query)
        if not tokens:
            logger.debug("table_exact_lookup: no meaningful tokens in query, skipping")
            return None

        logger.debug(
            f"table_exact_lookup: scanning {len(table_records)} table(s) "
            f"for tokens {tokens!r} in source {source_id}"
        )

        # --- 5. Scan rows for cell-level matches ------------------------------
        # Collect (table_record, matching_row) pairs so we can group output per table.
        matched: list[tuple[SourceTable, dict]] = []

        for tbl in table_records:
            headers = tbl.column_headers
            if not headers:
                continue
            for row in tbl.row_data:
                # Check each cell value against every candidate token.
                for cell_value in row.values():
                    cell_lower = str(cell_value).lower().strip()
                    if any(token == cell_lower or token in cell_lower for token in tokens):
                        matched.append((tbl, row))
                        break  # Only add each row once per table record.

        if not matched:
            logger.debug(
                f"table_exact_lookup: no matching rows found in source {source_id}"
            )
            return None

        # --- 6. Build Markdown output ----------------------------------------
        # Group rows by table so headers are printed once per table section.
        sections: list[str] = []
        # Preserve insertion order: track which tables we've opened.
        seen_table_ids: list[str] = []
        groups: dict[str, tuple[SourceTable, list[dict]]] = {}

        for tbl, row in matched:
            tid = tbl.table_id
            if tid not in groups:
                groups[tid] = (tbl, [])
                seen_table_ids.append(tid)
            groups[tid][1].append(row)

        for tid in seen_table_ids:
            tbl, rows = groups[tid]
            label_parts = [f"Table: {tbl.table_id}"]
            if tbl.page_number is not None:
                label_parts.append(f"Page: {tbl.page_number}")
            if tbl.sheet_name:
                label_parts.append(f"Sheet: {tbl.sheet_name}")
            sections.append("**" + " | ".join(label_parts) + "**")
            sections.append(_build_markdown_table(tbl.column_headers, rows))

        result = "\n\n".join(sections)
        logger.info(
            f"table_exact_lookup: found {len(matched)} matching row(s) "
            f"across {len(groups)} table(s) for source {source_id}"
        )
        return result

    except Exception as exc:
        logger.warning(
            f"table_exact_lookup: unexpected error for source {source_id}: {exc}. "
            "Falling through to vector retrieval."
        )
        return None
