"""
table_lookup.py — Two-phase row lookup for structured sources.

Scope:
  - Called from source_chat.py and ask.py for source-scoped table lookup.
  - Eligible for ANY source type that has source_table records
    (CSV, XLSX, DOCX, PDF, etc.).  No file-extension guard.

Algorithm — Phase 1 (keyword pre-filter):
  1. Fetch all source_table records for the given source_id.
  2. Check source file extension — bail out early if not .csv / .xlsx.
  3. Tokenise the query into candidate lookup words (strip stop-words).
  4. Scan every row_data dict in every table record for case-insensitive cell
     value matches against any candidate word.
  5. If any rows matched, render and return a GFM Markdown table immediately.

Algorithm — Phase 2 (semantic fallback, §3 upgrade):
  6. Triggered ONLY when Phase 1 returns zero rows AND source_table records exist.
  7. Generate a query embedding via the configured embedding model.
  8. Fetch source_embedding records where chunk_type = 'table_row' for this source.
  9. Compute cosine similarity (numpy) between the query embedding and each row
     embedding.
  10. Keep rows with score >= TABLE_LOOKUP_SIMILARITY_THRESHOLD (default 0.4).
  11. Return the top-10 scoring rows, reconstructed from the already-fetched
      SourceTable records using row_index (no additional DB call needed).

Guarantees:
  - If the source has no source_table records, returns None.
  - If Phase 1 hits, Phase 2 is never executed.
  - If the embedding model call fails, Phase 2 returns None gracefully.
  - If both phases miss, None is returned.
  - Output format is identical to Phase 1 (GFM Markdown, same grouping logic),
    so source_chat.py requires no changes.
  - No new dependencies; numpy is already a project dependency.
"""

from __future__ import annotations

import re
from typing import Optional

import numpy as np
from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import Source, SourceTable
from open_notebook.utils.embedding import generate_embedding

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

# (Evidence v2: _STRUCTURED_EXTENSIONS and _is_structured_source removed.
#  Eligibility is now determined solely by source_table records existing.)

# Maximum number of rows returned by the semantic fallback.
_SEMANTIC_TOP_N: int = 10


def _tokenize_query(query: str) -> list[str]:
    """Return non-stop-word tokens from *query* in lower-case."""
    raw_tokens = re.findall(r"\b[A-Za-z0-9_\-\.]+\b", query)
    return [t.lower() for t in raw_tokens if t.lower() not in _STOP_WORDS and len(t) > 1]


def _build_markdown_table(headers: list[str], rows: list[dict]) -> str:
    """Render *headers* and *rows* as a GFM Markdown table string."""
    header_row = "| " + " | ".join(str(h) for h in headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    data_rows = []
    for row in rows:
        cells = [str(row.get(h, "")) for h in headers]
        data_rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header_row, separator] + data_rows)





def _build_markdown_sections(matched: list[tuple[SourceTable, dict]]) -> str:
    """
    Group (table, row) pairs by table and render grouped GFM Markdown sections.

    Shared by Phase 1 and Phase 2 to guarantee identical output format.
    """
    sections: list[str] = []
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

    return "\n\n".join(sections)


async def _semantic_fallback(
    query: str,
    source_id: str,
    table_records: list[SourceTable],
) -> Optional[str]:
    """
    Phase 2 semantic fallback: embed the query, compute cosine similarity against
    pre-computed table-row embeddings stored in source_embedding, and return the
    top-N matching rows reconstructed from the already-fetched SourceTable records.

    Requirements:
    - Only called when Phase 1 returns zero rows AND table_records is non-empty.
    - source_embedding.content is plain text (the chunk text), NOT a dict.
    - Row data is retrieved from row_data[row_index] in the SourceTable records
      — no second DB call for row data reconstruction.
    - Returns None gracefully on any error (embedding failure, no matches, etc.).
    """
    from open_notebook.config import TABLE_LOOKUP_SIMILARITY_THRESHOLD

    # 1. Generate query embedding — any exception here → return None safely.
    try:
        query_embedding = await generate_embedding(query)
    except Exception as exc:
        logger.debug(
            f"table_lookup semantic fallback: embedding generation failed "
            f"for source {source_id}: {exc}"
        )
        return None

    query_vec = np.array(query_embedding, dtype=np.float64)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        logger.debug(
            "table_lookup semantic fallback: zero-norm query embedding, skipping"
        )
        return None
    query_vec = query_vec / query_norm

    # 2. Fetch table-row source_embedding records for this source.
    try:
        raw_embeddings = await repo_query(
            "SELECT id, embedding, table_id, row_index, page_number, sheet_name "
            "FROM source_embedding "
            "WHERE source = $source_id AND chunk_type = 'table_row'",
            {"source_id": ensure_record_id(source_id)},
        )
    except Exception as exc:
        logger.debug(
            f"table_lookup semantic fallback: DB query failed "
            f"for source {source_id}: {exc}"
        )
        return None

    if not raw_embeddings:
        logger.debug(
            f"table_lookup semantic fallback: no table_row embeddings "
            f"found for source {source_id}"
        )
        return None

    # 3. Build a lookup dict: table_id → SourceTable for O(1) row access.
    table_by_id: dict[str, SourceTable] = {tbl.table_id: tbl for tbl in table_records}

    # 4. Score each embedding row using cosine similarity.
    scored: list[tuple[float, SourceTable, dict]] = []

    for emb_record in raw_embeddings:
        raw_vec = emb_record.get("embedding")
        if not raw_vec:
            continue

        table_id = emb_record.get("table_id", "")
        row_index = emb_record.get("row_index")

        tbl = table_by_id.get(table_id)
        if tbl is None or row_index is None:
            # Embedding refers to a table we don't have in memory — skip.
            continue

        if row_index < 0 or row_index >= len(tbl.row_data):
            # row_index out of range for this table's row_data list.
            continue

        try:
            vec = np.array(raw_vec, dtype=np.float64)
            norm = np.linalg.norm(vec)
            if norm == 0:
                continue
            vec = vec / norm
            score = float(np.dot(query_vec, vec))
        except Exception:
            continue

        if score >= TABLE_LOOKUP_SIMILARITY_THRESHOLD:
            row = tbl.row_data[row_index]
            scored.append((score, tbl, row))

    if not scored:
        logger.debug(
            f"table_lookup semantic fallback: no rows above threshold "
            f"({TABLE_LOOKUP_SIMILARITY_THRESHOLD}) for source {source_id}"
        )
        return None

    # 5. Sort by score descending and cap at top-N.
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:_SEMANTIC_TOP_N]

    # 6. Build matched list for the shared renderer.
    matched: list[tuple[SourceTable, dict]] = [(tbl, row) for _, tbl, row in top]

    result = _build_markdown_sections(matched)
    logger.info(
        f"table_lookup semantic fallback: returned {len(top)} row(s) "
        f"for source {source_id} "
        f"(top score={top[0][0]:.3f}, threshold={TABLE_LOOKUP_SIMILARITY_THRESHOLD})"
    )
    return result


async def table_exact_lookup(query: str, source_id: str) -> Optional[str]:
    """
    Two-phase row-level lookup against extracted table data for *source_id*.

    Phase 1 — keyword pre-filter (deterministic, zero latency):
      Scans row_data cell values for exact/substring token matches.

    Phase 2 — semantic embedding fallback (only when Phase 1 returns nothing):
      Computes cosine similarity of the query embedding against pre-computed
      table-row embeddings. Returns top-10 rows above threshold 0.4 (configurable
      via TABLE_LOOKUP_SIMILARITY_THRESHOLD env var).

    Returns a GFM Markdown table of all matching rows across all source_table
    records, or ``None`` if:
      - No source_table records exist for this source
      - No cell value or embedding matches any candidate from *query*

    Raises:
        Nothing — all exceptions are caught and logged; ``None`` is returned
        on any error so source_chat falls through to vector-based context.
    """
    if not query or not source_id:
        return None

    try:
        # --- 1. Load the parent source to verify it exists --------------------
        source = await Source.get(source_id)
        if source is None:
            logger.debug(f"table_exact_lookup: source {source_id} not found, skipping")
            return None

        # --- 2. Fetch source_table records (any source type) ------------------
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

        # --- 5. Phase 1 — keyword scan ----------------------------------------
        matched: list[tuple[SourceTable, dict]] = []

        for tbl in table_records:
            headers = tbl.column_headers
            if not headers:
                continue
            for row in tbl.row_data:
                for cell_value in row.values():
                    cell_lower = str(cell_value).lower().strip()
                    if any(token == cell_lower or token in cell_lower for token in tokens):
                        matched.append((tbl, row))
                        break  # Only add each row once per table record.

        if matched:
            result = _build_markdown_sections(matched)
            logger.info(
                f"table_exact_lookup [Phase 1]: found {len(matched)} matching row(s) "
                f"across {len({tbl.table_id for tbl, _ in matched})} table(s) "
                f"for source {source_id}"
            )
            return result

        # --- 6. Phase 2 — semantic fallback -----------------------------------
        logger.debug(
            f"table_exact_lookup: Phase 1 miss for source {source_id}; "
            "trying semantic fallback"
        )
        return await _semantic_fallback(query, source_id, table_records)

    except Exception as exc:
        logger.warning(
            f"table_exact_lookup: unexpected error for source {source_id}: {exc}. "
            "Falling through to vector retrieval."
        )
        return None
