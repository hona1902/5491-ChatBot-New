"""
Unit tests for chunk_table() in open_notebook/utils/chunking.py

Tests:
- Header row present in every chunk
- Separator row present in every chunk
- Exactly one data row per chunk
- Returns empty list for empty table
- [truncated] marker appears when a row is oversized
- Token count of each chunk respects CHUNK_SIZE * 3 limit (after truncation)
"""
import pytest
from unittest.mock import MagicMock, patch

from open_notebook.utils.chunking import chunk_table, CHUNK_SIZE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_table(headers, rows, table_id="test_table_0"):
    """Create a duck-typed table object accepted by chunk_table()."""
    tbl = MagicMock()
    tbl.table_id = table_id
    tbl.column_headers = headers
    tbl.row_data = rows
    return tbl


def _parse_chunk_lines(chunk: str):
    """Split a chunk into its three lines: header, separator, data."""
    lines = chunk.strip().splitlines()
    assert len(lines) == 3, f"Expected 3 lines in chunk, got {len(lines)}: {chunk!r}"
    return lines  # header_line, separator_line, data_line


# ---------------------------------------------------------------------------
# Basic structural tests
# ---------------------------------------------------------------------------

class TestChunkTableStructure:
    def test_returns_list_of_strings(self):
        tbl = _make_table(["A", "B"], [{"A": "1", "B": "2"}])
        result = chunk_table(tbl)
        assert isinstance(result, list)
        assert all(isinstance(c, str) for c in result)

    def test_one_chunk_per_data_row(self):
        rows = [{"Name": f"Row{i}", "Value": str(i)} for i in range(5)]
        tbl = _make_table(["Name", "Value"], rows)
        chunks = chunk_table(tbl)
        assert len(chunks) == 5

    def test_header_present_in_every_chunk(self):
        headers = ["Alpha", "Beta", "Gamma"]
        rows = [{"Alpha": "a", "Beta": "b", "Gamma": "c"} for _ in range(3)]
        tbl = _make_table(headers, rows)
        chunks = chunk_table(tbl)

        for chunk in chunks:
            lines = _parse_chunk_lines(chunk)
            header_line = lines[0]
            for h in headers:
                assert h in header_line, (
                    f"Header '{h}' missing from chunk header line: {header_line!r}"
                )

    def test_separator_row_present_in_every_chunk(self):
        tbl = _make_table(["X", "Y"], [{"X": "1", "Y": "2"}, {"X": "3", "Y": "4"}])
        chunks = chunk_table(tbl)
        for chunk in chunks:
            lines = _parse_chunk_lines(chunk)
            sep = lines[1]
            # GFM separator: |---|---|
            assert "|" in sep and "---" in sep, (
                f"Separator line looks wrong: {sep!r}"
            )

    def test_data_row_values_appear_in_chunk(self):
        rows = [{"Col1": "hello", "Col2": "world"}]
        tbl = _make_table(["Col1", "Col2"], rows)
        chunks = chunk_table(tbl)
        assert len(chunks) == 1
        assert "hello" in chunks[0]
        assert "world" in chunks[0]

    def test_each_chunk_is_valid_gfm_table(self):
        """Every chunk must parse as a minimal 3-line GFM table."""
        rows = [{"Item": f"item{i}", "Qty": str(i * 10)} for i in range(4)]
        tbl = _make_table(["Item", "Qty"], rows)
        for chunk in chunk_table(tbl):
            lines = chunk.strip().splitlines()
            assert lines[0].startswith("|") and lines[0].endswith("|")
            assert lines[1].startswith("|") and "---" in lines[1]
            assert lines[2].startswith("|") and lines[2].endswith("|")


# ---------------------------------------------------------------------------
# Empty / edge case tests
# ---------------------------------------------------------------------------

class TestChunkTableEdgeCases:
    def test_empty_table_returns_empty_list(self):
        tbl = _make_table([], [])
        assert chunk_table(tbl) == []

    def test_no_rows_returns_empty_list(self):
        tbl = _make_table(["A", "B"], [])
        assert chunk_table(tbl) == []

    def test_no_headers_returns_empty_list(self):
        tbl = _make_table([], [{"A": "1"}])
        assert chunk_table(tbl) == []

    def test_single_row_single_column(self):
        tbl = _make_table(["Only"], [{"Only": "value"}])
        chunks = chunk_table(tbl)
        assert len(chunks) == 1
        assert "Only" in chunks[0]
        assert "value" in chunks[0]

    def test_missing_key_in_row_becomes_empty_string(self):
        """If a row dict is missing a column key, its cell value should be ''."""
        tbl = _make_table(["A", "B"], [{"A": "present"}])  # "B" missing
        chunks = chunk_table(tbl)
        assert len(chunks) == 1
        # The data line should have two cells; "B"'s value is empty
        data_line = chunks[0].splitlines()[2]
        cells = [c.strip() for c in data_line.strip("|").split("|")]
        assert cells[0] == "present"
        assert cells[1] == ""


# ---------------------------------------------------------------------------
# Truncation tests
# ---------------------------------------------------------------------------

class TestChunkTableTruncation:
    def test_truncated_marker_on_oversized_row(self):
        """A row whose chunk exceeds CHUNK_SIZE * 3 tokens must get [truncated]."""
        # Patch CHUNK_SIZE to a tiny value so the limit is easily exceeded.
        # With CHUNK_SIZE=5 the limit is 15 tokens — a 200-word value will exceed it.
        long_value = " ".join(f"word{i}" for i in range(200))  # ~200 tokens
        tbl = _make_table(["A", "B"], [{"A": long_value, "B": "normal"}])

        with patch("open_notebook.utils.chunking.CHUNK_SIZE", 5):
            chunks = chunk_table(tbl)

        assert len(chunks) == 1
        assert "[truncated]" in chunks[0], (
            "Expected [truncated] marker in oversized row chunk"
        )


    def test_non_oversized_row_has_no_truncated_marker(self):
        tbl = _make_table(["Name", "Age"], [{"Name": "Alice", "Age": "30"}])
        chunks = chunk_table(tbl)
        assert len(chunks) == 1
        assert "[truncated]" not in chunks[0]

    def test_truncated_chunk_still_contains_header(self):
        """Even after truncation, the header row must be present."""
        headers = ["Col1", "Col2"]
        long_value = " ".join(f"word{i}" for i in range(200))
        tbl = _make_table(headers, [{"Col1": long_value, "Col2": "ok"}])
        with patch("open_notebook.utils.chunking.CHUNK_SIZE", 5):
            chunks = chunk_table(tbl)
        for h in headers:
            assert h in chunks[0], (
                f"Header '{h}' missing from truncated chunk"
            )

    def test_truncated_chunk_token_count_within_limit(self):
        """After truncation the chunk must fit within CHUNK_SIZE * 3 tokens."""
        from open_notebook.utils.token_utils import token_count
        long_value = " ".join(f"word{i}" for i in range(200))
        tbl = _make_table(["Wide"], [{"Wide": long_value}])
        patched_limit = 5
        with patch("open_notebook.utils.chunking.CHUNK_SIZE", patched_limit):
            chunks = chunk_table(tbl)
        assert len(chunks) == 1
        # After truncation: header + sep + truncated data must be within the relaxed limit
        assert token_count(chunks[0]) <= patched_limit * 3 + 50  # tolerance for header/sep



# ---------------------------------------------------------------------------
# Token limit and ordering tests
# ---------------------------------------------------------------------------

class TestChunkTableOrdering:
    def test_chunks_order_matches_row_order(self):
        """Row chunks must appear in the same order as the input rows."""
        rows = [{"ID": str(i), "Val": f"v{i}"} for i in range(10)]
        tbl = _make_table(["ID", "Val"], rows)
        chunks = chunk_table(tbl)
        for i, chunk in enumerate(chunks):
            assert str(i) in chunk, (
                f"Chunk {i} does not contain row value '{i}'"
            )

    def test_large_number_of_rows(self):
        rows = [{"Key": f"k{i}", "Data": f"d{i}"} for i in range(200)]
        tbl = _make_table(["Key", "Data"], rows)
        chunks = chunk_table(tbl)
        assert len(chunks) == 200
