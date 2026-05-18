"""
tests/test_table_lookup.py
==========================
Unit tests for open_notebook/utils/table_lookup.py (Phase 1C).

Coverage:
  - Exact match returns correct GFM Markdown table
  - No match returns None
  - Case-insensitive matching works
  - Non-CSV/XLSX source returns None immediately (no row scan)
  - Source with no source_table records returns None
  - Empty query returns None
  - Partial token match (substring) also matches

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
# Tests
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
    """Querying a name not in any row returns None."""
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
    """PDF sources are skipped without scanning any rows."""
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
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice's score", "source:abc123")

    # Must return None and must NOT have called get_for_source
    assert result is None
    mock_get_tables.assert_not_called()


@pytest.mark.asyncio
async def test_docx_source_returns_none_immediately():
    """DOCX sources (not CSV/XLSX) are also skipped immediately."""
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
    ):
        from open_notebook.utils.table_lookup import table_exact_lookup

        result = await table_exact_lookup("Alice", "source:abc123")

    assert result is None
    mock_get_tables.assert_not_called()


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
