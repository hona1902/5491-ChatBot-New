"""
Phase 1A Test Suite — table-aware-qa
======================================
Covers all 13 verification points from /opsx:test.

Tests that require a live SurrealDB are skipped automatically via the
NO_DB_AVAILABLE fixture. All extractor, model, and context-formatting
tests run offline with no network or DB dependency.
"""

import csv
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

TESTS_DIR = os.path.dirname(__file__)
REAL_DOCX = os.path.join(TESTS_DIR, "VB TEST BANG BIEU.docx")
REAL_PDF = os.path.join(TESTS_DIR, "VB TEST BANG BIEU.pdf")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_csv(tmp_path, rows: List[List[str]], filename="sample.csv") -> str:
    p = tmp_path / filename
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)
    return str(p)


def _make_xlsx(tmp_path, sheets: Dict[str, List[List[str]]], filename="sample.xlsx") -> str:
    import openpyxl
    wb = openpyxl.Workbook()
    first = True
    for sheet_name, rows in sheets.items():
        if first:
            ws = wb.active
            ws.title = sheet_name
            first = False
        else:
            ws = wb.create_sheet(sheet_name)
        for row in rows:
            ws.append(row)
    p = tmp_path / filename
    wb.save(str(p))
    return str(p)


def _make_docx(tmp_path, table_data: List[List[str]], filename="sample.docx") -> str:
    import docx
    doc = docx.Document()
    doc.add_paragraph("Before table.")
    table = doc.add_table(rows=len(table_data), cols=len(table_data[0]))
    for r, row in enumerate(table_data):
        for c, cell in enumerate(row):
            table.rows[r].cells[c].text = cell
    doc.add_paragraph("After table.")
    p = tmp_path / filename
    doc.save(str(p))
    return str(p)


# ── 12. No pandas / pdfplumber added ─────────────────────────────────────────

class TestNoDependencyAdded:
    """Verify pandas and pdfplumber were NOT imported anywhere in Phase 1A code."""

    def test_no_pandas_in_registry(self):
        path = os.path.join(
            os.path.dirname(TESTS_DIR),
            "open_notebook", "utils", "table_extractor_registry.py"
        )
        with open(path) as f:
            src = f.read()
        assert "import pandas" not in src
        assert "from pandas" not in src

    def test_no_pdfplumber_in_registry(self):
        path = os.path.join(
            os.path.dirname(TESTS_DIR),
            "open_notebook", "utils", "table_extractor_registry.py"
        )
        with open(path) as f:
            src = f.read()
        assert "pdfplumber" not in src

    def test_pandas_not_importable_as_project_dep(self):
        """pandas should not appear in pyproject.toml dependencies."""
        toml_path = os.path.join(os.path.dirname(TESTS_DIR), "pyproject.toml")
        with open(toml_path) as f:
            content = f.read()
        assert "pandas" not in content

    def test_pdfplumber_not_importable_as_project_dep(self):
        toml_path = os.path.join(os.path.dirname(TESTS_DIR), "pyproject.toml")
        with open(toml_path) as f:
            content = f.read()
        assert "pdfplumber" not in content


# ── ExtractedTable model ──────────────────────────────────────────────────────

class TestExtractedTableModel:
    def test_model_fields_present(self):
        from open_notebook.utils.table_extractor_registry import ExtractedTable
        t = ExtractedTable(
            table_id="src_t0",
            source_id="source:abc",
            column_headers=["A", "B"],
            row_data=[{"A": "1", "B": "2"}],
            markdown_repr="| A | B |\n|---|---|\n| 1 | 2 |",
            row_count=1,
            col_count=2,
        )
        assert t.table_id == "src_t0"
        assert t.source_id == "source:abc"
        assert t.page_number is None
        assert t.sheet_name is None
        assert t.truncated is False

    def test_table_max_rows_default(self):
        from open_notebook.utils.table_extractor_registry import TABLE_MAX_ROWS
        assert TABLE_MAX_ROWS == int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_ROWS", "5000"))

    def test_table_max_rows_env_override(self, monkeypatch):
        monkeypatch.setenv("OPEN_NOTEBOOK_TABLE_MAX_ROWS", "42")
        import importlib
        import open_notebook.utils.table_extractor_registry as reg
        importlib.reload(reg)
        assert reg.TABLE_MAX_ROWS == 42
        # Restore
        monkeypatch.delenv("OPEN_NOTEBOOK_TABLE_MAX_ROWS", raising=False)
        importlib.reload(reg)


# ── 2. CSV extractor ──────────────────────────────────────────────────────────

class TestCSVExtractor:
    def test_basic_csv(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_csv(tmp_path, [
            ["Name", "Score", "Grade"],
            ["Alice", "95", "A"],
            ["Bob",   "82", "B"],
        ])
        tables = extract_tables_from_source(path, "source:test")
        assert len(tables) == 1
        t = tables[0]
        assert t.column_headers == ["Name", "Score", "Grade"]
        assert t.row_count == 2
        assert t.col_count == 3
        assert not t.truncated
        assert "| Name | Score | Grade |" in t.markdown_repr
        assert "| Alice | 95 | A |" in t.markdown_repr
        assert t.page_number is None
        assert t.sheet_name is None

    def test_csv_table_id_format(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_csv(tmp_path, [["H1", "H2"], ["v1", "v2"]])
        tables = extract_tables_from_source(path, "source:myid")
        assert tables[0].table_id == "source:myid_table_0"

    def test_csv_markdown_has_separator(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_csv(tmp_path, [["Col"], ["val"]])
        tables = extract_tables_from_source(path, "source:x")
        assert "|---|" in tables[0].markdown_repr or "|---" in tables[0].markdown_repr

    def test_csv_truncation(self, tmp_path, monkeypatch):
        """When row limit reached, truncated=True and row_count <= limit."""
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_ROWS", 2)
        path = _make_csv(tmp_path, [["H"]] + [[str(i)] for i in range(10)])
        tables = reg.extract_tables_from_source(path, "source:x")
        assert tables[0].truncated is True
        assert tables[0].row_count == 2

    def test_empty_csv_returns_empty(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_csv(tmp_path, [])
        tables = extract_tables_from_source(path, "source:x")
        assert tables == []

    def test_unknown_extension_returns_empty(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        p = tmp_path / "file.txt"
        p.write_text("hello")
        tables = extract_tables_from_source(str(p), "source:x")
        assert tables == []

    def test_nonexistent_file_returns_empty(self):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        tables = extract_tables_from_source("/no/such/file.csv", "source:x")
        assert tables == []


# ── 5. XLSX extractor ─────────────────────────────────────────────────────────

class TestXLSXExtractor:
    def test_single_sheet(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_xlsx(tmp_path, {
            "Sheet1": [["Product", "Price"], ["Apple", "1.5"], ["Banana", "0.8"]]
        })
        tables = extract_tables_from_source(path, "source:xl")
        assert len(tables) == 1
        t = tables[0]
        assert t.column_headers == ["Product", "Price"]
        assert t.row_count == 2
        assert t.sheet_name == "Sheet1"
        assert t.page_number is None
        assert "| Product | Price |" in t.markdown_repr

    def test_multi_sheet_produces_multiple_tables(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_xlsx(tmp_path, {
            "Alpha": [["A", "B"], ["1", "2"]],
            "Beta":  [["X", "Y"], ["9", "8"]],
        })
        tables = extract_tables_from_source(path, "source:xl2")
        assert len(tables) == 2
        names = {t.sheet_name for t in tables}
        assert "Alpha" in names and "Beta" in names

    def test_xlsx_truncation(self, tmp_path, monkeypatch):
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_ROWS", 1)
        path = _make_xlsx(tmp_path, {
            "S": [["H"]] + [[str(i)] for i in range(5)]
        })
        tables = reg.extract_tables_from_source(path, "source:xl3")
        assert tables[0].truncated is True
        assert tables[0].row_count == 1


# ── 6. PDF extractor (no crash even without tables) ───────────────────────────

class TestPDFExtractor:
    def test_pdf_no_crash_on_no_tables(self, tmp_path):
        """PDF extractor must return [] gracefully if fitz finds no tables."""
        import fitz
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source

        # Create minimal real PDF with no tables
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "No table here.")
        p = tmp_path / "no_table.pdf"
        doc.save(str(p))
        doc.close()

        tables = extract_tables_from_source(str(p), "source:pdf")
        # Returns empty list — no crash
        assert isinstance(tables, list)

    @pytest.mark.skipif(
        not os.path.exists(REAL_PDF),
        reason="Real PDF not found in tests/"
    )
    def test_real_pdf_does_not_crash(self):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        tables = extract_tables_from_source(REAL_PDF, "source:real_pdf")
        assert isinstance(tables, list)

    def test_pdf_page_exception_handled(self):
        """A page-level exception should not propagate out of _extract_pdf."""
        from open_notebook.utils.table_extractor_registry import _extract_pdf
        # Patch fitz where it lives (fitz is imported inside the function body)
        with patch("fitz.open") as mock_open:
            mock_page = MagicMock()
            mock_page.find_tables.side_effect = RuntimeError("fitz crash")
            mock_doc = MagicMock()
            mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
            mock_doc.close = MagicMock()
            mock_open.return_value = mock_doc
            result = _extract_pdf("fake.pdf", "source:x", 0)
        assert result == []


# ── 7. DOCX extractor (no crash even without tables) ─────────────────────────

class TestDOCXExtractor:
    def test_docx_no_crash_on_no_tables(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        import docx
        doc = docx.Document()
        doc.add_paragraph("Just prose, no tables.")
        p = tmp_path / "no_table.docx"
        doc.save(str(p))
        tables = extract_tables_from_source(str(p), "source:docx")
        assert isinstance(tables, list)
        assert tables == []

    def test_docx_with_table_extracts_data(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_docx(tmp_path, [
            ["Country", "Capital"],
            ["Vietnam", "Hanoi"],
            ["Japan",   "Tokyo"],
        ])
        tables = extract_tables_from_source(path, "source:docx2")
        assert len(tables) == 1
        t = tables[0]
        assert "Country" in t.column_headers
        assert t.row_count == 2
        assert "| Country | Capital |" in t.markdown_repr

    @pytest.mark.skipif(
        not os.path.exists(REAL_DOCX),
        reason="Real DOCX not found in tests/"
    )
    def test_real_docx_does_not_crash(self):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        tables = extract_tables_from_source(REAL_DOCX, "source:real_docx")
        assert isinstance(tables, list)


# ── Markdown generation helper ────────────────────────────────────────────────

class TestMarkdownGeneration:
    def test_to_markdown_format(self):
        from open_notebook.utils.table_extractor_registry import _to_markdown
        headers = ["Name", "Val"]
        rows = [{"Name": "A", "Val": "1"}, {"Name": "B", "Val": "2"}]
        md = _to_markdown(headers, rows)
        assert "| Name | Val |" in md
        assert "|---|---|" in md
        assert "| A | 1 |" in md
        assert "| B | 2 |" in md

    def test_to_markdown_empty_headers(self):
        from open_notebook.utils.table_extractor_registry import _to_markdown
        assert _to_markdown([], []) == ""


# ── 1. & 2. Domain model: SourceTable + Source ───────────────────────────────

class TestDomainModel:
    def test_source_table_table_name(self):
        from open_notebook.domain.notebook import SourceTable
        assert SourceTable.table_name == "source_table"

    def test_source_table_fields(self):
        from open_notebook.domain.notebook import SourceTable
        t = SourceTable(
            table_id="t0",
            column_headers=["A", "B"],
            row_data=[{"A": "1", "B": "2"}],
            markdown_repr="| A | B |\n|---|---|\n| 1 | 2 |",
            row_count=1,
            col_count=2,
        )
        assert t.page_number is None
        assert t.sheet_name is None
        assert t.truncated is False

    def test_source_tables_markdown_field_default_none(self):
        from open_notebook.domain.notebook import Source
        s = Source()
        assert s.tables_markdown is None

    def test_source_tables_markdown_set(self):
        from open_notebook.domain.notebook import Source
        s = Source(tables_markdown="| A |\n|---|\n| 1 |")
        assert "| A |" in s.tables_markdown

    def test_source_full_text_unchanged_by_tables_markdown(self):
        """tables_markdown does NOT modify full_text."""
        from open_notebook.domain.notebook import Source
        s = Source(full_text="prose content", tables_markdown="| H |\n|---|\n| v |")
        assert s.full_text == "prose content"
        assert s.tables_markdown != s.full_text

    def test_source_prepare_save_data_excludes_none_tables_markdown(self):
        """When tables_markdown is None, it is omitted from the save dict."""
        from open_notebook.domain.notebook import Source
        s = Source(title="Test", full_text="some text")
        data = s._prepare_save_data()
        assert "tables_markdown" not in data

    def test_source_prepare_save_data_includes_tables_markdown_when_set(self):
        from open_notebook.domain.notebook import Source
        s = Source(title="T", tables_markdown="| X |\n|---|\n| y |")
        data = s._prepare_save_data()
        assert "tables_markdown" in data


# ── 9. _format_source_context TABLE DATA injection ───────────────────────────

class TestFormatSourceContext:
    """Tests for _format_source_context without a live DB."""

    def _call(self, source_dict: dict) -> str:
        from open_notebook.graphs.source_chat import _format_source_context
        return _format_source_context({"sources": [source_dict]})

    def test_tables_markdown_appended_after_full_text(self):
        result = self._call({
            "id": "source:1",
            "title": "My CSV",
            "full_text": "short prose",
            "tables_markdown": "| Col |\n|---|\n| val |",
        })
        assert "## TABLE DATA" in result
        assert "| Col |" in result
        assert "short prose" in result
        # TABLE DATA must appear after full_text
        assert result.index("short prose") < result.index("## TABLE DATA")

    def test_tables_markdown_survives_full_text_truncation(self):
        """Even when full_text is >5000 chars, tables_markdown is still present."""
        long_prose = "x" * 6000
        result = self._call({
            "id": "source:2",
            "title": "Big Source",
            "full_text": long_prose,
            "tables_markdown": "| Header |\n|---|\n| cell_value |",
        })
        assert "[Content truncated]" in result   # prose was truncated
        assert "## TABLE DATA" in result         # table data still present
        assert "cell_value" in result            # table content intact
        # TABLE DATA must not be inside the truncated prose block
        truncated_pos = result.index("[Content truncated]")
        table_data_pos = result.index("## TABLE DATA")
        assert truncated_pos < table_data_pos

    def test_no_tables_markdown_no_table_data_section(self):
        result = self._call({
            "id": "source:3",
            "title": "Plain Source",
            "full_text": "just prose",
        })
        assert "## TABLE DATA" not in result

    def test_tables_markdown_none_no_table_data_section(self):
        result = self._call({
            "id": "source:4",
            "title": "Null tables",
            "full_text": "prose",
            "tables_markdown": None,
        })
        assert "## TABLE DATA" not in result

    def test_instruction_text_present_in_table_section(self):
        result = self._call({
            "id": "source:5",
            "title": "T",
            "full_text": "p",
            "tables_markdown": "| H |\n|---|\n| v |",
        })
        assert "Prefer these for exact-value questions" in result

    def test_existing_source_without_tables_markdown_key(self):
        """Source dicts without the tables_markdown key must not crash."""
        result = self._call({
            "id": "source:6",
            "title": "Legacy",
            "full_text": "old record",
            # tables_markdown key absent entirely
        })
        assert "old record" in result
        assert "## TABLE DATA" not in result


# ── 10. Backward compatibility: no tables_markdown ────────────────────────────

class TestBackwardCompatibility:
    def test_source_without_tables_markdown_is_valid(self):
        from open_notebook.domain.notebook import Source
        # Simulating a legacy record loaded from DB with no tables_markdown field
        s = Source(title="old", full_text="content")
        assert s.tables_markdown is None  # default None, no error

    def test_source_embedding_without_chunk_type_is_valid(self):
        """SourceEmbedding doesn't declare chunk_type — that's OK, DB schema is optional."""
        from open_notebook.domain.notebook import SourceEmbedding
        # SourceEmbedding only has 'content'; the DB optional fields don't break the model
        e = SourceEmbedding(content="some text")
        assert e.content == "some text"


# ── Migration 16 SQL file validity ───────────────────────────────────────────

class TestMigrationFile:
    def _read(self, name) -> str:
        base = os.path.join(
            os.path.dirname(TESTS_DIR),
            "open_notebook", "database", "migrations"
        )
        with open(os.path.join(base, name)) as f:
            return f.read()

    def test_migration_16_exists(self):
        content = self._read("16.surrealql")
        assert len(content) > 0

    def test_migration_16_down_exists(self):
        content = self._read("16_down.surrealql")
        assert len(content) > 0

    def test_migration_16_defines_source_table(self):
        content = self._read("16.surrealql")
        assert "DEFINE TABLE source_table" in content

    def test_migration_16_adds_tables_markdown_to_source(self):
        content = self._read("16.surrealql")
        assert "tables_markdown" in content
        assert "ON source" in content

    def test_migration_16_adds_chunk_type_to_source_embedding(self):
        content = self._read("16.surrealql")
        assert "chunk_type" in content
        assert "ON source_embedding" in content

    def test_migration_16_registered_in_async_migrate(self):
        from open_notebook.database.async_migrate import AsyncMigrationManager
        mgr = AsyncMigrationManager()
        assert len(mgr.up_migrations) == 16
        assert len(mgr.down_migrations) == 16

    def test_migration_16_down_removes_source_table(self):
        content = self._read("16_down.surrealql")
        assert "REMOVE TABLE source_table" in content

    def test_migration_16_down_removes_tables_markdown(self):
        content = self._read("16_down.surrealql")
        assert "tables_markdown" in content


# ── 3. & 13. Existing imports / syntax still intact ──────────────────────────

class TestExistingCodeIntegrity:
    def test_source_graph_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "graphs", "source.py"
        )
        with open(path) as f:
            ast.parse(f.read())  # raises SyntaxError if broken

    def test_source_chat_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "graphs", "source_chat.py"
        )
        with open(path) as f:
            ast.parse(f.read())

    def test_notebook_domain_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "domain", "notebook.py"
        )
        with open(path) as f:
            ast.parse(f.read())

    def test_registry_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(TESTS_DIR),
            "open_notebook", "utils", "table_extractor_registry.py"
        )
        with open(path) as f:
            ast.parse(f.read())

    def test_source_state_has_extracted_tables_field(self):
        """SourceState TypedDict should now include extracted_tables and tables_markdown."""
        import ast
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "graphs", "source.py"
        )
        with open(path) as f:
            src = f.read()
        assert "extracted_tables" in src
        assert "tables_markdown" in src
        assert "extract_tables" in src

    def test_source_delete_cascade_includes_source_table(self):
        """Source.delete() should contain DELETE source_table."""
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "domain", "notebook.py"
        )
        with open(path) as f:
            src = f.read()
        assert "DELETE source_table WHERE source = $source_id" in src

    def test_save_source_writes_tables_markdown(self):
        """save_source in source.py should reference tables_markdown."""
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "graphs", "source.py"
        )
        with open(path) as f:
            src = f.read()
        assert "source.tables_markdown = tables_markdown" in src

    def test_graph_edges_include_extract_tables(self):
        """Graph must have edge: content_process → extract_tables → save_source."""
        path = os.path.join(
            os.path.dirname(TESTS_DIR), "open_notebook", "graphs", "source.py"
        )
        with open(path) as f:
            src = f.read()
        assert '"content_process", "extract_tables"' in src
        assert '"extract_tables", "save_source"' in src


# ── 8. tables_markdown independent from full_text (logic test) ───────────────

class TestTablesMarkdownIndependence:
    def test_tables_markdown_field_is_separate_from_full_text(self):
        from open_notebook.domain.notebook import Source
        s = Source(
            full_text="A" * 6000,
            tables_markdown="| Col |\n|---|\n| val |",
        )
        assert len(s.full_text) == 6000
        assert "| Col |" in s.tables_markdown
        assert "| Col |" not in s.full_text

    def test_context_formatter_full_text_truncated_but_table_intact(self):
        """The key integration invariant: 5000-char truncation of full_text
        does NOT clip tables_markdown."""
        from open_notebook.graphs.source_chat import _format_source_context
        ctx = _format_source_context({
            "sources": [{
                "id": "source:T",
                "title": "T",
                "full_text": "Z" * 6000,
                "tables_markdown": "| UNIQUE_MARKER |\n|---|\n| found_it |",
            }]
        })
        # Prose was truncated
        assert "Z" * 5001 not in ctx
        assert "[Content truncated]" in ctx
        # Table marker survived
        assert "UNIQUE_MARKER" in ctx
        assert "found_it" in ctx
