"""
tests/test_table_lookup.py
==========================
Unit tests for open_notebook/utils/table_lookup.py.

Coverage (Phase 1C — keyword lookup):
  - Exact match returns correct GFM Markdown table
  - No match returns None
  - Case-insensitive matching works
  - Non-CSV/XLSX source returns None immediately (no row scan)
  - Source with no source_table records returns None
  - Empty query returns None
  - Partial token match (substring) also matches
  - Output includes page/sheet metadata labels
  - Unexpected exception in Source.get returns None
  - All-stop-word query returns None

Coverage (Phase 2 — semantic fallback, §3):
  - Keyword hit → Phase 2 is never called (no embedding generated)
  - Semantic hit for synonym/conceptual query → row returned
  - Both Phase 1 and Phase 2 miss → None returned
  - Non-CSV source → embedding generation never called
  - Embedding/model error → None returned safely
  - Score below threshold → None returned
  - Top-N cap: returns at most 10 rows
  - Multi-table grouping preserves table/page/sheet metadata

All DB/domain calls are mocked with AsyncMock so no database is required.
"""

from __future__ import annotations

from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — build lightweight stubs that mimic domain objects
# ---------------------------------------------------------------------------


def _make_asset(file_path: Optional[str] = None):
    asset = MagicMock()
    asset.file_path = file_path
    return asset


def _make_source(source_id: str = "source:abc123", file_path: Optional[str] = None):
    source = MagicMock()
    source.id = source_id
    source.asset = _make_asset(file_path)
    return source


def _make_table(
    table_id: str,
    column_headers: List[str],
    row_data: List[dict],
    page_number: Optional[int] = None,
    sheet_name: Optional[str] = None,
):
    tbl = MagicMock()
    tbl.table_id = table_id
    tbl.column_headers = column_headers
    tbl.row_data = row_data
    tbl.page_number = page_number
    tbl.sheet_name = sheet_name
    return tbl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CSV_HEADERS = ["Name", "Score", "Grade"]
CSV_ROWS = [
    {"Name": "Alice", "Score": "95", "Grade": "A"},
    {"Name": "Bob", "Score": "82", "Grade": "B"},
    {"Name": "Charlie", "Score": "70", "Grade": "C"},
]


# ---------------------------------------------------------------------------
# Phase 1C — keyword lookup tests (unchanged from original)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_match_returns_markdown():
    """Querying 'Alice' against a CSV source returns a Markdown table with Alice's row."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("What is Alice's score?", "source:abc123")

    assert result is not None
    assert "Alice" in result
    assert "95" in result
    # Should NOT contain Bob (no match)
    assert "Bob" not in result


@pytest.mark.asyncio
async def test_no_match_returns_none():
    """Querying a name not in any row returns None (with no semantic embeddings present)."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    # Semantic fallback will query DB for embeddings → return empty list → None
    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup._semantic_fallback",
            new=AsyncMock(return_value=None),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup(
            "What is Diana's score?", "source:abc123"
        )

    assert result is None


@pytest.mark.asyncio
async def test_case_insensitive_matching():
    """Cell matching is case-insensitive: 'alice' matches row with 'Alice'."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("show me alice's grade", "source:abc123")

    assert result is not None
    assert "Alice" in result


@pytest.mark.asyncio
async def test_non_csv_source_returns_none_immediately():
    """PDF sources are skipped without scanning any rows or calling embeddings."""
    source = _make_source(file_path="/data/report.pdf")

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[]),
        ) as mock_get_tables,
        patch(
            "open_notebook.utils.table_lookup._semantic_fallback",
            new=AsyncMock(return_value=None),
        ) as mock_semantic,
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice's score", "source:abc123")

    # Must return None and must NOT have called get_for_source or semantic fallback
    assert result is None
    mock_get_tables.assert_not_called()
    mock_semantic.assert_not_called()


@pytest.mark.asyncio
async def test_docx_source_returns_none_immediately():
    """DOCX sources (not CSV/XLSX) are also skipped immediately — no embedding call."""
    source = _make_source(file_path="/data/document.docx")

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[]),
        ) as mock_get_tables,
        patch(
            "open_notebook.utils.table_lookup._semantic_fallback",
            new=AsyncMock(return_value=None),
        ) as mock_semantic,
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice", "source:abc123")

    assert result is None
    mock_get_tables.assert_not_called()
    mock_semantic.assert_not_called()


@pytest.mark.asyncio
async def test_xlsx_source_is_eligible():
    """XLSX sources are eligible for exact lookup (not skipped on extension check)."""
    source = _make_source(file_path="/data/budget.xlsx")
    table = _make_table(
        "t1",
        ["Item", "Cost"],
        [{"Item": "Laptop", "Cost": "1200"}, {"Item": "Monitor", "Cost": "400"}],
        sheet_name="Q1",
    )

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("cost of laptop", "source:abc123")

    assert result is not None
    assert "Laptop" in result
    assert "1200" in result


@pytest.mark.asyncio
async def test_no_source_table_records_returns_none():
    """CSV source with no extracted table records returns None."""
    source = _make_source(file_path="/data/empty.csv")

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[]),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice", "source:abc123")

    assert result is None


@pytest.mark.asyncio
async def test_empty_query_returns_none():
    """An empty query string returns None without touching the database."""
    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(),
        ) as mock_get,
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(),
        ) as mock_tables,
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("", "source:abc123")

    assert result is None
    mock_get.assert_not_called()
    mock_tables.assert_not_called()


@pytest.mark.asyncio
async def test_source_not_found_returns_none():
    """If Source.get returns None, lookup returns None gracefully."""
    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=None),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice", "source:missing")

    assert result is None


@pytest.mark.asyncio
async def test_output_includes_page_and_sheet_labels():
    """Returned Markdown table section label includes page/sheet metadata."""
    source = _make_source(file_path="/data/survey.xlsx")
    table = _make_table(
        "survey_t1",
        ["Region", "Revenue"],
        [{"Region": "North", "Revenue": "500000"}],
        page_number=2,
        sheet_name="Summary",
    )

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("revenue north", "source:abc123")

    assert result is not None
    assert "survey_t1" in result
    assert "Page: 2" in result
    assert "Sheet: Summary" in result


@pytest.mark.asyncio
async def test_error_in_source_get_returns_none():
    """An unexpected exception in Source.get is swallowed and returns None."""
    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(side_effect=RuntimeError("DB connection lost")),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice", "source:abc123")

    assert result is None


@pytest.mark.asyncio
async def test_all_stop_words_query_returns_none():
    """A query composed entirely of stop-words produces no tokens and returns None.

    Note: the implementation loads source_table records before running tokenisation
    (so it can still short-circuit on empty tables for other code paths). The
    important guarantee is that the result is None — no false matches are returned.
    """
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        # "what is the" — all stop-words → no candidate tokens → no row matches
        result = await table_exact_lookup("what is the", "source:abc123")

    # Must return None: no tokens means no cell-value matches possible
    assert result is None


# ---------------------------------------------------------------------------
# Phase 2 — semantic fallback tests (§3 tasks 3.3–3.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_hit_does_not_call_semantic_fallback():
    """Task 3.3: When Phase 1 keyword match succeeds, _semantic_fallback is never called."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup._semantic_fallback",
            new=AsyncMock(return_value="SHOULD NOT APPEAR"),
        ) as mock_semantic,
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("What is Alice's score?", "source:abc123")

    # Phase 1 hit — semantic must never be called
    assert result is not None
    assert "Alice" in result
    mock_semantic.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_fallback_returns_synonym_row():
    """Task 3.4: When keyword misses, semantic fallback returns a conceptually matching row."""
    import numpy as np

    source = _make_source(file_path="/data/scores.csv")
    table = _make_table(
        "t1",
        ["Product", "Revenue"],
        [
            {"Product": "Widget A", "Revenue": "12000"},
            {"Product": "Gadget B", "Revenue": "8000"},
        ],
    )

    # Simulate a query embedding and stored row embedding that are similar.
    query_emb = [1.0, 0.0, 0.0]  # unit vector
    row_emb_high = [0.95, 0.1, 0.0]  # close enough (cosine > 0.4)
    row_emb_low = [0.0, 1.0, 0.0]  # orthogonal (cosine ≈ 0)

    # Normalise for expected dot products:
    # row_emb_high normalised dot query_emb ≈ 0.994  → above threshold
    # row_emb_low dot query_emb = 0.0                → below threshold

    stored_embeddings = [
        {
            "id": "source_embedding:1",
            "embedding": row_emb_high,
            "table_id": "t1",
            "row_index": 0,
            "page_number": None,
            "sheet_name": None,
        },
        {
            "id": "source_embedding:2",
            "embedding": row_emb_low,
            "table_id": "t1",
            "row_index": 1,
            "page_number": None,
            "sheet_name": None,
        },
    ]

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup.generate_embedding",
            new=AsyncMock(return_value=query_emb),
        ),
        patch(
            "open_notebook.utils.table_lookup.repo_query",
            new=AsyncMock(return_value=stored_embeddings),
        ),
        patch(
            "open_notebook.utils.table_lookup.ensure_record_id",
            side_effect=lambda x: x,
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        # "earnings" is a synonym for "Revenue" — keyword won't match
        result = await table_exact_lookup("show earnings by product", "source:abc123")

    assert result is not None
    # Widget A (row_index=0) should appear; Gadget B (row_index=1) should not
    assert "Widget A" in result
    assert "Gadget B" not in result


@pytest.mark.asyncio
async def test_both_phases_miss_returns_none():
    """Task 3.5: When keyword AND semantic both miss, None is returned."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    # Semantic fallback: all stored embeddings have low cosine similarity
    orthogonal_emb = [0.0, 1.0, 0.0]
    stored_embeddings = [
        {
            "id": "source_embedding:1",
            "embedding": orthogonal_emb,
            "table_id": "t1",
            "row_index": 0,
            "page_number": None,
            "sheet_name": None,
        },
    ]

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup.generate_embedding",
            new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
        ),
        patch(
            "open_notebook.utils.table_lookup.repo_query",
            new=AsyncMock(return_value=stored_embeddings),
        ),
        patch(
            "open_notebook.utils.table_lookup.ensure_record_id",
            side_effect=lambda x: x,
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        # "Xenon" doesn't match any cell; cosine ≈ 0 (below 0.4 threshold)
        result = await table_exact_lookup("Xenon plasma score", "source:abc123")

    assert result is None


@pytest.mark.asyncio
async def test_non_csv_source_does_not_call_embedding():
    """Task 3.6: Non-CSV/XLSX sources return None immediately, no embedding call made."""
    source = _make_source(file_path="/data/report.pdf")

    # We'll assert generate_embedding is never imported/called.
    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[]),
        ) as mock_get_tables,
        # Patch the embedding function at both possible call sites
        patch(
            "open_notebook.utils.table_lookup._semantic_fallback",
            new=AsyncMock(return_value=None),
        ) as mock_semantic,
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("some query about data", "source:abc123")

    assert result is None
    mock_get_tables.assert_not_called()
    mock_semantic.assert_not_called()


@pytest.mark.asyncio
async def test_embedding_error_returns_none():
    """Task 3.7 (error path): Embedding model failure → None returned, no crash."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup.generate_embedding",
            new=AsyncMock(side_effect=RuntimeError("Model unavailable")),
        ),
        patch(
            "open_notebook.utils.table_lookup.repo_query",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "open_notebook.utils.table_lookup.ensure_record_id",
            side_effect=lambda x: x,
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        # "Xenon" misses keyword phase; semantic model raises → safe None
        result = await table_exact_lookup("Xenon plasma levels", "source:abc123")

    assert result is None


@pytest.mark.asyncio
async def test_score_below_threshold_returns_none():
    """Score below TABLE_LOOKUP_SIMILARITY_THRESHOLD → row excluded → None."""
    source = _make_source(file_path="/data/scores.csv")
    table = _make_table("t1", CSV_HEADERS, CSV_ROWS)

    # Orthogonal embedding → cosine = 0.0, which is < 0.4 threshold
    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup.generate_embedding",
            new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
        ),
        patch(
            "open_notebook.utils.table_lookup.repo_query",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "source_embedding:1",
                        "embedding": [0.0, 1.0, 0.0],  # orthogonal → score = 0
                        "table_id": "t1",
                        "row_index": 0,
                        "page_number": None,
                        "sheet_name": None,
                    }
                ]
            ),
        ),
        patch(
            "open_notebook.utils.table_lookup.ensure_record_id",
            side_effect=lambda x: x,
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Xenon gas measurement", "source:abc123")

    assert result is None


@pytest.mark.asyncio
async def test_top_n_cap_returns_at_most_10_rows():
    """Top-N cap: semantic fallback returns at most 10 rows even if more qualify."""
    # Build a table with 15 rows
    headers = ["Item", "Value"]
    rows = [{"Item": f"item_{i}", "Value": str(i * 10)} for i in range(15)]
    source = _make_source(file_path="/data/large.csv")
    table = _make_table("t1", headers, rows)

    # All 15 embeddings have high similarity (parallel to query)
    high_similarity_emb = [1.0, 0.0, 0.0]
    stored_embeddings = [
        {
            "id": f"source_embedding:{i}",
            "embedding": high_similarity_emb,
            "table_id": "t1",
            "row_index": i,
            "page_number": None,
            "sheet_name": None,
        }
        for i in range(15)
    ]

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table]),
        ),
        patch(
            "open_notebook.utils.table_lookup.generate_embedding",
            new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
        ),
        patch(
            "open_notebook.utils.table_lookup.repo_query",
            new=AsyncMock(return_value=stored_embeddings),
        ),
        patch(
            "open_notebook.utils.table_lookup.ensure_record_id",
            side_effect=lambda x: x,
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        # Query that misses keyword phase (no item_X token match)
        result = await table_exact_lookup("conceptual synonym query", "source:abc123")

    assert result is not None
    # Count how many data rows appear in the result (each row is one pipe-delimited line)
    # Header row + separator row + up to 10 data rows = at most 12 lines in the table block
    data_lines = [
        line for line in result.splitlines()
        if line.startswith("|") and "---" not in line and "Item" not in line
    ]
    assert len(data_lines) <= 10, f"Expected ≤10 rows but got {len(data_lines)}"


@pytest.mark.asyncio
async def test_multi_table_semantic_grouping_preserves_metadata():
    """Semantic results from multiple tables render with correct table/page/sheet labels."""
    headers = ["Country", "Population"]
    source = _make_source(file_path="/data/world.xlsx")

    # Two tables: one with page metadata, one with sheet metadata
    table_a = _make_table(
        "world_t1",
        headers,
        [{"Country": "France", "Population": "67M"}],
        page_number=1,
        sheet_name=None,
    )
    table_b = _make_table(
        "world_t2",
        headers,
        [{"Country": "Germany", "Population": "84M"}],
        page_number=None,
        sheet_name="Europe",
    )

    high_emb = [1.0, 0.0, 0.0]
    stored_embeddings = [
        {
            "id": "source_embedding:1",
            "embedding": high_emb,
            "table_id": "world_t1",
            "row_index": 0,
            "page_number": 1,
            "sheet_name": None,
        },
        {
            "id": "source_embedding:2",
            "embedding": high_emb,
            "table_id": "world_t2",
            "row_index": 0,
            "page_number": None,
            "sheet_name": "Europe",
        },
    ]

    with (
        patch(
            "open_notebook.utils.table_lookup.Source.get",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "open_notebook.utils.table_lookup.SourceTable.get_for_source",
            new=AsyncMock(return_value=[table_a, table_b]),
        ),
        patch(
            "open_notebook.utils.table_lookup.generate_embedding",
            new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
        ),
        patch(
            "open_notebook.utils.table_lookup.repo_query",
            new=AsyncMock(return_value=stored_embeddings),
        ),
        patch(
            "open_notebook.utils.table_lookup.ensure_record_id",
            side_effect=lambda x: x,
        ),
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        # "inhabitants" is conceptually related to "Population" but won't keyword-match
        result = await table_exact_lookup("inhabitants by nation", "source:abc123")

    assert result is not None
    # Both table labels must appear with their metadata
    assert "world_t1" in result
    assert "Page: 1" in result
    assert "world_t2" in result
    assert "Sheet: Europe" in result
    # Both country rows must be present
    assert "France" in result
    assert "Germany" in result
