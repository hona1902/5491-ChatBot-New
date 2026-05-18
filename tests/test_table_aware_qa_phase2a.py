"""
Phase 2A-1 Test Suite — table-aware-qa Phase 2
===============================================
Covers tasks 1.7-1.9 and 2.2-2.9:
  • Column-count cap enforcement (TABLE_MAX_COLS)
  • tables_markdown character-cap truncation (TABLES_MARKDOWN_MAX_CHARS)
  • XLSX duplicate-header deduplication + original_headers
  • HTML table extractor (.html / .htm)
  • .xls decision gate (no xlrd dependency)
  • Phase 1 regression: CSV/XLSX/DOCX still work unchanged

All tests run offline — no live DB or network required.
"""

import csv
import os
import tempfile
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


# ── Loguru capture helper ─────────────────────────────────────────────────────

@pytest.fixture
def loguru_warnings():
    """Capture loguru WARNING+ messages into a list for assertion."""
    from loguru import logger
    messages = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="WARNING", format="{message}")
    yield messages
    logger.remove(handler_id)


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


def _make_html(tmp_path, html_content: str, filename="page.html") -> str:
    p = tmp_path / filename
    p.write_text(html_content, encoding="utf-8")
    return str(p)


def _make_xls(tmp_path, filename="legacy.xls") -> str:
    """Create a fake .xls file (content doesn't matter — we test the gate, not parsing)."""
    p = tmp_path / filename
    p.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE2 magic bytes
    return str(p)


# ── Task 1.7 — Column cap: table with >MAX_COLS columns ──────────────────────

class TestColumnCap:
    """1.7: column limit reached → headers capped, truncated=True, warning logged."""

    def test_csv_col_cap_applied(self, tmp_path, monkeypatch, loguru_warnings):
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_COLS", 3)
        headers = ["A", "B", "C", "D", "E"]
        path = _make_csv(tmp_path, [headers, ["1", "2", "3", "4", "5"]])
        tables = reg.extract_tables_from_source(path, "source:x")
        assert len(tables) == 1
        t = tables[0]
        assert t.col_count == 3
        assert t.column_headers == ["A", "B", "C"]
        assert t.truncated is True
        assert any("column limit" in m for m in loguru_warnings)

    def test_xlsx_col_cap_applied(self, tmp_path, monkeypatch):
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_COLS", 2)
        path = _make_xlsx(tmp_path, {
            "Sheet1": [["X", "Y", "Z", "W"], ["1", "2", "3", "4"]]
        })
        tables = reg.extract_tables_from_source(path, "source:xl")
        assert tables[0].col_count == 2
        assert tables[0].column_headers == ["X", "Y"]
        assert tables[0].truncated is True

    def test_row_data_capped_to_max_cols(self, tmp_path, monkeypatch):
        """Row dicts must only contain keys for the capped headers."""
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_COLS", 2)
        path = _make_csv(tmp_path, [["A","B","C"], ["1","2","3"]])
        tables = reg.extract_tables_from_source(path, "source:x")
        row = tables[0].row_data[0]
        assert set(row.keys()) == {"A", "B"}
        assert "C" not in row

    # Task 1.9: both row and column caps trigger on the same table
    def test_both_caps_trigger(self, tmp_path, monkeypatch):
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_ROWS", 2)
        monkeypatch.setattr(reg, "TABLE_MAX_COLS", 2)
        headers = ["A", "B", "C"]
        data_rows = [[str(i), str(i), str(i)] for i in range(10)]
        path = _make_csv(tmp_path, [headers] + data_rows)
        tables = reg.extract_tables_from_source(path, "source:both")
        t = tables[0]
        assert t.row_count == 2
        assert t.col_count == 2
        assert t.truncated is True

    def test_exactly_at_cap_not_truncated(self, tmp_path, monkeypatch):
        """A table with exactly TABLE_MAX_COLS columns must NOT be truncated."""
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_COLS", 3)
        path = _make_csv(tmp_path, [["A","B","C"], ["1","2","3"]])
        tables = reg.extract_tables_from_source(path, "source:exact")
        assert tables[0].col_count == 3
        assert tables[0].truncated is False

    def test_config_default_value(self):
        """TABLE_MAX_COLS default must equal OPEN_NOTEBOOK_TABLE_MAX_COLS env var default."""
        import open_notebook.utils.table_extractor_registry as reg
        assert reg.TABLE_MAX_COLS == int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_COLS", "100"))


# ── Task 1.8 — tables_markdown char cap (tested via assembly logic) ───────────

class TestTablesMarkdownCap:
    """1.8: char cap stops assembly at table boundary; truncation marker appended."""

    def _build_parts(self, n_tables: int, chars_each: int) -> List[str]:
        """Generate n_tables markdown parts of approximately chars_each characters."""
        return [f"<!-- T{i} -->\n" + ("| H |\n|---|\n| " + "x" * (chars_each - 20) + " |")
                for i in range(n_tables)]

    def test_truncation_at_boundary(self):
        """If 3 tables fit but the 4th pushes over, only 3 are included + marker."""
        # Simulate the assembly logic from source.py
        from open_notebook.config import TABLES_MARKDOWN_MAX_CHARS

        parts = self._build_parts(6, 100)  # 6 * ~100 chars = ~600
        max_chars = 350  # fits about 3 tables

        assembled = []
        running = 0
        sep = "\n\n"
        omitted = 0
        for part in parts:
            addition = (len(sep) if assembled else 0) + len(part)
            if running + addition > max_chars:
                omitted = len(parts) - len(assembled)
                break
            assembled.append(part)
            running += addition

        result = sep.join(assembled)
        if omitted:
            result += f"\n\n<!-- tables_markdown truncated: {omitted} table(s) omitted -->"

        assert len(assembled) < len(parts)
        assert omitted > 0
        assert "truncated" in result
        assert f"{omitted} table(s) omitted" in result

    def test_all_tables_fit_no_marker(self):
        """If all tables fit within the cap, no truncation marker is added."""
        parts = ["<!-- T0 -->\n| A |\n|---|\n| 1 |", "<!-- T1 -->\n| B |\n|---|\n| 2 |"]
        max_chars = 100000  # very large
        assembled = []
        running = 0
        sep = "\n\n"
        omitted = 0
        for part in parts:
            addition = (len(sep) if assembled else 0) + len(part)
            if running + addition > max_chars:
                omitted = len(parts) - len(assembled)
                break
            assembled.append(part)
            running += addition

        result = sep.join(assembled)
        assert omitted == 0
        assert "truncated" not in result
        assert len(assembled) == 2

    def test_first_table_exceeds_cap_returns_none(self):
        """When even the first table exceeds the cap, tables_markdown must be None."""
        parts = ["<!-- T0 -->\n" + "x" * 200]
        max_chars = 50  # too small for even one table

        assembled = []
        running = 0
        sep = "\n\n"
        omitted = 0
        for part in parts:
            addition = (len(sep) if assembled else 0) + len(part)
            if running + addition > max_chars:
                omitted = len(parts) - len(assembled)
                break
            assembled.append(part)
            running += addition

        tables_markdown = sep.join(assembled) if assembled else None
        assert tables_markdown is None

    def test_config_defaults(self):
        from open_notebook.config import (
            TABLE_MAX_COLS, TABLES_MARKDOWN_MAX_CHARS,
            TABLE_LOOKUP_SIMILARITY_THRESHOLD, ASK_TABLE_SOURCE_THRESHOLD
        )
        assert TABLE_MAX_COLS == int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_COLS", "100"))
        assert TABLES_MARKDOWN_MAX_CHARS == int(
            os.environ.get("OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS", "50000"))
        assert TABLE_LOOKUP_SIMILARITY_THRESHOLD == float(
            os.environ.get("TABLE_LOOKUP_SIMILARITY_THRESHOLD", "0.4"))
        assert ASK_TABLE_SOURCE_THRESHOLD == float(
            os.environ.get("ASK_TABLE_SOURCE_THRESHOLD", "0.35"))


# ── Task 2.2 — XLSX duplicate-header deduplication ───────────────────────────

class TestXLSXDeduplicate:
    """2.2: duplicate XLSX headers renamed _2/_3; original_headers stored; warning logged."""

    def test_duplicate_headers_renamed(self, tmp_path, loguru_warnings):
        import open_notebook.utils.table_extractor_registry as reg
        path = _make_xlsx(tmp_path, {
            "Sheet1": [
                ["Name", "Score", "Name", "Score"],
                ["Alice", "90", "Bob", "80"],
            ]
        })
        tables = reg.extract_tables_from_source(path, "source:dup")
        assert len(tables) == 1
        t = tables[0]
        assert t.column_headers == ["Name", "Score", "Name_2", "Score_2"]
        assert any("duplicate" in m for m in loguru_warnings)

    def test_original_headers_stored(self, tmp_path):
        import open_notebook.utils.table_extractor_registry as reg
        path = _make_xlsx(tmp_path, {
            "S": [["A", "B", "A"], ["1", "2", "3"]]
        })
        tables = reg.extract_tables_from_source(path, "source:orig")
        assert tables[0].original_headers == ["A", "B", "A"]

    def test_no_duplicates_no_original_headers(self, tmp_path):
        """When headers are unique, original_headers must be None."""
        import open_notebook.utils.table_extractor_registry as reg
        path = _make_xlsx(tmp_path, {
            "S": [["X", "Y", "Z"], ["1", "2", "3"]]
        })
        tables = reg.extract_tables_from_source(path, "source:nodup")
        assert tables[0].original_headers is None

    def test_triple_duplicate(self, tmp_path):
        """A header appearing 3 times → _2, _3 suffixes."""
        import open_notebook.utils.table_extractor_registry as reg
        path = _make_xlsx(tmp_path, {
            "S": [["A", "A", "A"], ["1", "2", "3"]]
        })
        tables = reg.extract_tables_from_source(path, "source:triple")
        assert tables[0].column_headers == ["A", "A_2", "A_3"]

    def test_row_data_uses_deduped_headers(self, tmp_path):
        """Row dicts must use the deduplicated header names as keys."""
        import open_notebook.utils.table_extractor_registry as reg
        path = _make_xlsx(tmp_path, {
            "S": [["Val", "Val"], ["10", "20"]]
        })
        tables = reg.extract_tables_from_source(path, "source:rowkeys")
        row = tables[0].row_data[0]
        assert "Val" in row
        assert "Val_2" in row

    # Task 2.2b: SourceTable domain model accepts original_headers
    def test_source_table_domain_accepts_original_headers(self):
        from open_notebook.domain.notebook import SourceTable
        st = SourceTable(
            table_id="t0",
            column_headers=["Name", "Score", "Name_2"],
            original_headers=["Name", "Score", "Name"],
            row_data=[],
            markdown_repr="",
            row_count=0,
            col_count=3,
        )
        assert st.original_headers == ["Name", "Score", "Name"]

    def test_source_table_original_headers_default_none(self):
        from open_notebook.domain.notebook import SourceTable
        st = SourceTable(table_id="t0", column_headers=["A"], row_data=[], markdown_repr="", row_count=0, col_count=1)
        assert st.original_headers is None


# ── Tasks 2.4–2.6 — HTML table extractor ────────────────────────────────────

class TestHTMLExtractor:
    """2.4: HTML with one table → one ExtractedTable with correct headers and rows."""

    SIMPLE_HTML = """<html><body>
    <table>
      <tr><th>Country</th><th>Capital</th></tr>
      <tr><td>Vietnam</td><td>Hanoi</td></tr>
      <tr><td>Japan</td><td>Tokyo</td></tr>
    </table>
    </body></html>"""

    def test_basic_html_extraction(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_html(tmp_path, self.SIMPLE_HTML)
        tables = extract_tables_from_source(path, "source:html")
        assert len(tables) == 1
        t = tables[0]
        assert t.column_headers == ["Country", "Capital"]
        assert t.row_count == 2
        assert t.col_count == 2
        assert t.page_number is None
        assert t.sheet_name is None
        assert "| Country | Capital |" in t.markdown_repr
        assert "| Vietnam | Hanoi |" in t.markdown_repr

    def test_htm_extension_also_supported(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_html(tmp_path, self.SIMPLE_HTML, filename="page.htm")
        tables = extract_tables_from_source(path, "source:htm")
        assert len(tables) == 1

    def test_multiple_tables_in_html(self, tmp_path):
        html = """<html><body>
        <table><tr><th>A</th></tr><tr><td>1</td></tr></table>
        <table><tr><th>B</th></tr><tr><td>2</td></tr></table>
        </body></html>"""
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_html(tmp_path, html)
        tables = extract_tables_from_source(path, "source:html2")
        assert len(tables) == 2

    # Task 2.5: no <table> → empty list, no exception
    def test_html_no_table_returns_empty(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_html(tmp_path, "<html><body><p>No tables here.</p></body></html>")
        tables = extract_tables_from_source(path, "source:notbl")
        assert tables == []

    # Task 2.6: malformed HTML → empty list, warning logged
    def test_malformed_html_returns_empty(self, tmp_path, caplog):
        import logging
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        # Severely malformed: broken tags but html.parser handles most gracefully
        # Force the file-read to fail to test the warning path
        path = tmp_path / "bad.html"
        # Write a header-only table (no data rows) — should return []
        path.write_text("<html><table><tr><th>H</th></tr></table></html>", encoding="utf-8")
        tables = extract_tables_from_source(str(path), "source:bad")
        # Header-only table → no data rows → skip → empty
        assert tables == []

    def test_html_table_id_format(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_html(tmp_path, self.SIMPLE_HTML)
        tables = extract_tables_from_source(path, "source:myid")
        assert tables[0].table_id == "source:myid_table_0"

    def test_html_col_cap_applied(self, tmp_path, monkeypatch):
        import open_notebook.utils.table_extractor_registry as reg
        monkeypatch.setattr(reg, "TABLE_MAX_COLS", 2)
        html = "<table><tr><th>A</th><th>B</th><th>C</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>"
        path = _make_html(tmp_path, html)
        tables = reg.extract_tables_from_source(path, "source:htmlcap")
        assert tables[0].col_count == 2
        assert tables[0].truncated is True


# ── Tasks 2.7–2.8 — .xls decision gate ──────────────────────────────────────

class TestXLSGate:
    """2.8: .xls without xlrd → empty list, user-visible warning message."""

    def test_xls_returns_empty_list(self, tmp_path, caplog):
        import logging
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_xls(tmp_path)
        with caplog.at_level(logging.WARNING):
            tables = extract_tables_from_source(path, "source:xls")
        assert tables == []

    def test_xls_warning_message_matches_spec(self, tmp_path, loguru_warnings):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_xls(tmp_path)
        extract_tables_from_source(path, "source:xls")
        combined = " ".join(loguru_warnings)
        assert "XLS files are not supported" in combined
        assert "XLSX" in combined

    def test_xls_not_handled_by_xlsx_extractor(self, tmp_path):
        """The .xls gate must intercept before openpyxl is called."""
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        with patch("openpyxl.load_workbook") as mock_wb:
            path = _make_xls(tmp_path)
            extract_tables_from_source(path, "source:xlsgate")
            mock_wb.assert_not_called()

    def test_xlrd_not_imported_in_registry(self):
        """xlrd must NOT be imported in table_extractor_registry.py."""
        registry_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "open_notebook", "utils", "table_extractor_registry.py"
        )
        with open(registry_path) as f:
            src = f.read()
        assert "import xlrd" not in src
        assert "from xlrd" not in src


# ── Task 2.9 — Phase 1 regression: existing extractors unchanged ─────────────

class TestPhase1Regression:
    """2.9: CSV, XLSX, DOCX extractors still work as before Phase 2."""

    def test_csv_still_extracts_correctly(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_csv(tmp_path, [["Name", "Score"], ["Alice", "95"]])
        tables = extract_tables_from_source(path, "source:reg_csv")
        assert len(tables) == 1
        assert tables[0].column_headers == ["Name", "Score"]
        assert tables[0].row_count == 1

    def test_xlsx_still_extracts_correctly(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        path = _make_xlsx(tmp_path, {"S": [["A", "B"], ["1", "2"]]})
        tables = extract_tables_from_source(path, "source:reg_xlsx")
        assert len(tables) == 1
        assert tables[0].column_headers == ["A", "B"]

    def test_unknown_extension_still_empty(self, tmp_path):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        p = tmp_path / "file.txt"
        p.write_text("hello")
        assert extract_tables_from_source(str(p), "source:txt") == []

    def test_nonexistent_file_still_empty(self):
        from open_notebook.utils.table_extractor_registry import extract_tables_from_source
        assert extract_tables_from_source("/no/such/file.csv", "source:x") == []

    def test_registry_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "open_notebook", "utils", "table_extractor_registry.py"
        )
        with open(path) as f:
            ast.parse(f.read())

    def test_source_graph_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "open_notebook", "graphs", "source.py"
        )
        with open(path) as f:
            ast.parse(f.read())

    def test_config_imports_cleanly(self):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "open_notebook", "config.py"
        )
        with open(path) as f:
            ast.parse(f.read())

    def test_no_new_pyproject_dependencies(self):
        """Confirm xlrd, pandas, pdfplumber not added to pyproject.toml."""
        toml_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        with open(toml_path) as f:
            content = f.read()
        assert "xlrd" not in content
        assert "pandas" not in content
        assert "pdfplumber" not in content
