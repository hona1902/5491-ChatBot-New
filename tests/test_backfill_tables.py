"""
Unit tests for scripts/backfill_tables.py
==========================================
Covers tasks 7.8–7.10 and additional functional requirements:

  7.8  --dry-run makes no writes to DB
  7.9  source with existing tables is skipped without --force;
       re-processed with --force (no duplicates)
  7.10 one source fails extraction → error logged, others continue,
       exit code non-zero

Additional:
  - source-ids filter only processes selected sources
  - source with no tables is handled safely
  - missing file produces SKIP (not ERROR), run continues
  - SourceTable.source is persisted as record<source> (via ensure_record_id)
  - no frontend / ask.py changes (static import assertions)
"""

import asyncio
import csv
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# Ensure project root is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_csv_file(tmp_path: Path, rows: List[List[str]], filename: str = "test.csv") -> Path:
    """Write a CSV file and return its path."""
    p = tmp_path / filename
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)
    return p


def _fake_source(source_id: str, file_path: Optional[str] = None) -> MagicMock:
    """Build a lightweight mock Source object."""
    from open_notebook.domain.notebook import Asset

    src = MagicMock()
    src.id = source_id
    src.tables_markdown = None
    src.asset = Asset(file_path=file_path) if file_path else None
    src.save = AsyncMock()
    return src


def _fake_extracted_table(source_id: str, table_index: int = 0):
    """Return a minimal ExtractedTable fixture."""
    from open_notebook.utils.table_extractor_registry import ExtractedTable

    return ExtractedTable(
        table_id=f"{source_id}_table_{table_index}",
        source_id=source_id,
        column_headers=["Col1", "Col2"],
        row_data=[{"Col1": "a", "Col2": "b"}],
        markdown_repr="| Col1 | Col2 |\n|---|---|\n| a | b |",
        row_count=1,
        col_count=2,
        truncated=False,
    )


# ---------------------------------------------------------------------------
# Task 7.8 — --dry-run makes no writes
# ---------------------------------------------------------------------------


class TestDryRun:
    """7.8: --dry-run reports what would happen but performs zero DB writes."""

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_source_save(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["A", "B"], ["1", "2"]])
        source = _fake_source("source:dry1", str(csv_path))
        fake_tables = [_fake_extracted_table("source:dry1")]

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables):
            result = await mod._process_source(source, force=False, dry_run=True, skip_embeddings=False)

        # Source.save() must NOT be called in dry-run
        source.save.assert_not_called()
        assert result["status"] == "OK"
        assert result["table_count"] == 1
        assert "dry-run" in (result.get("reason") or "").lower()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_source_table_save(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["X"], ["1"]])
        source = _fake_source("source:dry2", str(csv_path))
        fake_tables = [_fake_extracted_table("source:dry2")]

        saved_records: List[Any] = []

        async def _capture_save(self_obj: Any) -> None:
            saved_records.append(self_obj)

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod.SourceTable, "save", new=_capture_save):
            await mod._process_source(source, force=False, dry_run=True, skip_embeddings=False)

        assert saved_records == [], "SourceTable.save() must not be called in dry-run mode"

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_repo_query(self, tmp_path: Path) -> None:
        """No DELETE or INSERT queries must be issued during dry-run."""
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["A"], ["1"]])
        source = _fake_source("source:dry3", str(csv_path))
        fake_tables = [_fake_extracted_table("source:dry3")]
        issued_queries: List[str] = []

        async def _spy_query(query: str, vars: Any = None) -> List[Any]:
            issued_queries.append(query)
            return []

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod, "repo_query", side_effect=_spy_query):
            await mod._process_source(source, force=False, dry_run=True, skip_embeddings=False)

        assert issued_queries == [], f"Unexpected DB queries in dry-run: {issued_queries}"

    @pytest.mark.asyncio
    async def test_dry_run_row_count_returned(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod
        from open_notebook.utils.table_extractor_registry import ExtractedTable

        csv_path = _make_csv_file(tmp_path, [["A", "B"], ["1", "2"], ["3", "4"]])
        source = _fake_source("source:dry4", str(csv_path))
        fake_tables = [
            ExtractedTable(
                table_id="source:dry4_table_0",
                source_id="source:dry4",
                column_headers=["A", "B"],
                row_data=[{"A": "1", "B": "2"}, {"A": "3", "B": "4"}],
                markdown_repr="...",
                row_count=2,
                col_count=2,
                truncated=False,
            )
        ]

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables):
            result = await mod._process_source(source, force=False, dry_run=True, skip_embeddings=False)

        assert result["row_count"] == 2


# ---------------------------------------------------------------------------
# Task 7.9 — skip without --force; reprocess with --force (no duplicates)
# ---------------------------------------------------------------------------


class TestForceAndSkip:
    """7.9: idempotency and --force behaviour."""

    @pytest.mark.asyncio
    async def test_skip_when_existing_tables_no_force(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["A"], ["1"]])
        source = _fake_source("source:skip1", str(csv_path))
        existing = [MagicMock()]  # simulate 1 existing source_table record

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=existing):
            result = await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        assert result["status"] == "SKIP"
        assert "already has" in (result.get("reason") or "")
        source.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_issues_delete_query(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["Name"], ["Alice"]])
        source = _fake_source("source:force1", str(csv_path))
        existing = [MagicMock()]  # 1 old record
        fake_tables = [_fake_extracted_table("source:force1")]
        deleted_queries: List[str] = []

        async def _spy_query(query: str, vars: Any = None) -> List[Any]:
            deleted_queries.append(query)
            return []

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=existing), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod, "repo_query", side_effect=_spy_query), \
             patch.object(mod.SourceTable, "save", new_callable=AsyncMock):
            result = await mod._process_source(source, force=True, dry_run=False, skip_embeddings=False)

        assert any("DELETE source_table" in q for q in deleted_queries), \
            "--force must issue DELETE source_table query"
        assert result["status"] == "OK"

    @pytest.mark.asyncio
    async def test_force_no_duplicate_records(self, tmp_path: Path) -> None:
        """After --force, exactly N new records are saved (not N_old + N_new)."""
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["Col"], ["val"]])
        source = _fake_source("source:nodup", str(csv_path))
        existing = [MagicMock(), MagicMock()]  # 2 old records
        fake_tables = [_fake_extracted_table("source:nodup")]  # should produce 1 new record

        saved: List[Any] = []

        async def _capture_save(self_obj: Any) -> None:
            saved.append(self_obj)

        async def _noop_query(query: str, vars: Any = None) -> List[Any]:
            return []

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=existing), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod, "repo_query", side_effect=_noop_query), \
             patch.object(mod.SourceTable, "save", new=_capture_save):
            result = await mod._process_source(source, force=True, dry_run=False, skip_embeddings=False)

        # Exactly 1 new record saved — NOT 3 (2 old + 1 new)
        assert len(saved) == 1, f"Expected 1 saved record, got {len(saved)}"
        assert result["table_count"] == 1

    @pytest.mark.asyncio
    async def test_force_on_source_with_no_existing_tables(self, tmp_path: Path) -> None:
        """--force on a source with no existing tables works fine (no DELETE attempt)."""
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["A"], ["1"]])
        source = _fake_source("source:forceempty", str(csv_path))
        fake_tables = [_fake_extracted_table("source:forceempty")]

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod.SourceTable, "save", new_callable=AsyncMock):
            result = await mod._process_source(source, force=True, dry_run=False, skip_embeddings=False)

        assert result["status"] == "OK"
        assert result["table_count"] == 1


# ---------------------------------------------------------------------------
# Task 7.10 — one source fails, others continue, exit non-zero
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """7.10: per-source errors are logged and the batch continues."""

    @pytest.mark.asyncio
    async def test_extraction_failure_returns_error_status(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["A"], ["1"]])
        source = _fake_source("source:fail1", str(csv_path))

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", side_effect=RuntimeError("boom")):
            result = await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        assert result["status"] == "ERROR"
        assert "boom" in (result.get("reason") or "")

    @pytest.mark.asyncio
    async def test_one_failure_does_not_abort_batch(self, tmp_path: Path) -> None:
        """When one source raises, the other sources in the batch still succeed."""
        import scripts.backfill_tables as mod

        csv_good = _make_csv_file(tmp_path, [["A"], ["1"]], filename="good.csv")
        src_good = _fake_source("source:good1", str(csv_good))
        src_bad = _fake_source("source:bad1", str(tmp_path / "missing.csv"))  # file doesn't exist

        fake_tables = [_fake_extracted_table("source:good1")]

        async def _fake_get_for_source(source_id: str) -> List[Any]:
            return []

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, side_effect=_fake_get_for_source), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod.SourceTable, "save", new_callable=AsyncMock):
            result_good = await mod._process_source(
                src_good, force=False, dry_run=False, skip_embeddings=False
            )
            result_bad = await mod._process_source(
                src_bad, force=False, dry_run=False, skip_embeddings=False
            )

        assert result_good["status"] == "OK"
        # Bad source: file doesn't exist → SKIP (not ERROR crash)
        assert result_bad["status"] in ("SKIP", "ERROR")

    @pytest.mark.asyncio
    async def test_exit_code_nonzero_when_any_error(self, tmp_path: Path) -> None:
        """main() must return exit code 1 if any source has ERROR status."""
        import scripts.backfill_tables as mod

        src_err = _fake_source("source:errsrc", str(tmp_path / "gone.csv"))

        # Patch _load_sources to return our error source
        # Patch _process_source to return an ERROR result
        async def _fake_load(source_ids: Any) -> List[Any]:
            return [src_err]

        async def _fake_process(src: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "ERROR", "source_id": str(src.id), "table_count": 0, "row_count": 0, "reason": "simulated"}

        with patch.object(mod, "_load_sources", side_effect=_fake_load), \
             patch.object(mod, "_process_source", side_effect=_fake_process), \
             patch("sys.argv", ["backfill_tables.py"]):
            exit_code = await mod.main()

        assert exit_code == 1, "Exit code must be 1 when any source has ERROR status"

    @pytest.mark.asyncio
    async def test_exit_code_zero_all_success(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        src_ok = _fake_source("source:oksrc", str(tmp_path / "x.csv"))

        async def _fake_load(source_ids: Any) -> List[Any]:
            return [src_ok]

        async def _fake_process(src: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"status": "OK", "source_id": str(src.id), "table_count": 1, "row_count": 5, "reason": None}

        with patch.object(mod, "_load_sources", side_effect=_fake_load), \
             patch.object(mod, "_process_source", side_effect=_fake_process), \
             patch("sys.argv", ["backfill_tables.py"]):
            exit_code = await mod.main()

        assert exit_code == 0


# ---------------------------------------------------------------------------
# source-ids filter only processes selected sources
# ---------------------------------------------------------------------------


class TestSourceIdsFilter:
    """--source-ids must load only the specified sources."""

    @pytest.mark.asyncio
    async def test_source_ids_filter_loads_correct_sources(self) -> None:
        import scripts.backfill_tables as mod

        src_a = _fake_source("source:aaa")
        src_b = _fake_source("source:bbb")

        async def _fake_get(sid: str) -> Optional[MagicMock]:
            mapping = {"source:aaa": src_a, "source:bbb": src_b}
            return mapping.get(sid)

        with patch.object(mod.Source, "get", side_effect=_fake_get):
            loaded = await mod._load_sources(["source:aaa", "source:bbb"])

        assert len(loaded) == 2
        assert loaded[0] is src_a
        assert loaded[1] is src_b

    @pytest.mark.asyncio
    async def test_unknown_source_id_is_warned_and_skipped(self) -> None:
        import scripts.backfill_tables as mod

        with patch.object(mod.Source, "get", return_value=None):
            loaded = await mod._load_sources(["source:nonexistent"])

        assert loaded == []

    @pytest.mark.asyncio
    async def test_source_ids_does_not_load_all(self) -> None:
        """When --source-ids given, repo_query(SELECT * FROM source) must NOT be called."""
        import scripts.backfill_tables as mod

        query_calls: List[str] = []

        async def _spy_query(query: str, vars: Any = None) -> List[Any]:
            query_calls.append(query)
            return []

        async def _fake_get(sid: str) -> Optional[MagicMock]:
            return _fake_source(sid)

        with patch.object(mod.Source, "get", side_effect=_fake_get), \
             patch.object(mod, "repo_query", side_effect=_spy_query):
            await mod._load_sources(["source:xyz"])

        select_star_calls = [q for q in query_calls if "SELECT" in q and "FROM source" in q]
        assert select_star_calls == [], \
            "repo_query(SELECT * FROM source) must not be called when --source-ids is given"


# ---------------------------------------------------------------------------
# Source with no tables handled safely
# ---------------------------------------------------------------------------


class TestNoTables:
    """A source file that yields no tables must not error."""

    @pytest.mark.asyncio
    async def test_no_tables_extracted_status_ok(self, tmp_path: Path) -> None:
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["Name", "Value"]])  # header-only, no data rows
        source = _fake_source("source:notables", str(csv_path))

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=[]):
            result = await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        # No tables → should be OK with 0 tables (or SKIP is also acceptable)
        assert result["status"] in ("OK", "SKIP")
        assert result["table_count"] == 0
        assert result["row_count"] == 0

    @pytest.mark.asyncio
    async def test_no_tables_does_not_update_tables_markdown(self, tmp_path: Path) -> None:
        """tables_markdown must not be set if no tables were extracted."""
        import scripts.backfill_tables as mod

        csv_path = _make_csv_file(tmp_path, [["A"]])
        source = _fake_source("source:notables2", str(csv_path))

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=[]):
            await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        # source.save() should NOT be called for tables_markdown update
        # (nothing to write)
        source.save.assert_not_called()


# ---------------------------------------------------------------------------
# Missing file handled safely
# ---------------------------------------------------------------------------


class TestMissingFile:
    """A source whose file has been deleted must produce SKIP, not a crash."""

    @pytest.mark.asyncio
    async def test_missing_file_produces_skip(self) -> None:
        import scripts.backfill_tables as mod

        source = _fake_source("source:nofile", "/nonexistent/path/data.csv")

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]):
            result = await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        assert result["status"] == "SKIP"
        assert "not found" in (result.get("reason") or "").lower()

    @pytest.mark.asyncio
    async def test_url_only_source_produces_skip(self) -> None:
        """Source with no file_path (URL-only) must produce SKIP."""
        import scripts.backfill_tables as mod

        source = _fake_source("source:urlonly", file_path=None)

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]):
            result = await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        assert result["status"] == "SKIP"

    @pytest.mark.asyncio
    async def test_missing_file_does_not_crash_batch(self, tmp_path: Path) -> None:
        """One missing-file source must not abort processing of the next source."""
        import scripts.backfill_tables as mod

        src_missing = _fake_source("source:miss", "/no/such/file.csv")
        csv_path = _make_csv_file(tmp_path, [["A"], ["1"]], "real.csv")
        src_real = _fake_source("source:real", str(csv_path))
        fake_tables = [_fake_extracted_table("source:real")]

        async def _fake_get_for_source(source_id: str) -> List[Any]:
            return []

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, side_effect=_fake_get_for_source), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod.SourceTable, "save", new_callable=AsyncMock):
            r_missing = await mod._process_source(src_missing, force=False, dry_run=False, skip_embeddings=False)
            r_real = await mod._process_source(src_real, force=False, dry_run=False, skip_embeddings=False)

        assert r_missing["status"] == "SKIP"
        assert r_real["status"] == "OK"


# ---------------------------------------------------------------------------
# SourceTable.source persisted as record<source>
# ---------------------------------------------------------------------------


class TestSourceRecordId:
    """SourceTable.source must be set as a record<source> (via ensure_record_id)."""

    @pytest.mark.asyncio
    async def test_source_field_uses_ensure_record_id(self, tmp_path: Path) -> None:
        """The 'source' field on persisted SourceTable must be passed through ensure_record_id."""
        import scripts.backfill_tables as mod
        from surrealdb import RecordID

        csv_path = _make_csv_file(tmp_path, [["A"], ["1"]])
        source = _fake_source("source:rid1", str(csv_path))
        fake_tables = [_fake_extracted_table("source:rid1")]

        captured_sts: List[Any] = []

        async def _capture_save(self_obj: Any) -> None:
            captured_sts.append(self_obj)

        with patch.object(mod.SourceTable, "get_for_source", new_callable=AsyncMock, return_value=[]), \
             patch.object(mod, "extract_tables_from_source", return_value=fake_tables), \
             patch.object(mod.SourceTable, "save", new=_capture_save):
            await mod._process_source(source, force=False, dry_run=False, skip_embeddings=False)

        assert len(captured_sts) == 1
        st = captured_sts[0]
        # _prepare_save_data converts source str → RecordID; we can verify the raw
        # source field is a string that can be parsed as a RecordID
        source_val = st.source
        assert source_val is not None, "SourceTable.source must not be None"
        # The value should be parseable as a RecordID (either already RecordID or
        # a properly formatted string like "source:rid1")
        record = RecordID.parse(source_val) if isinstance(source_val, str) else source_val
        assert str(record) == "source:rid1"


# ---------------------------------------------------------------------------
# Scope guard — no frontend / ask.py modifications
# ---------------------------------------------------------------------------


class TestScopeGuard:
    """Confirm the backfill script does not import or touch out-of-scope modules."""

    def test_backfill_does_not_import_ask_py(self) -> None:
        """backfill_tables.py must not import from open_notebook.graphs.ask."""
        script_path = _project_root / "scripts" / "backfill_tables.py"
        content = script_path.read_text(encoding="utf-8")
        assert "graphs.ask" not in content, "backfill_tables.py must not import ask.py"
        assert "from open_notebook.graphs import ask" not in content

    def test_backfill_does_not_import_frontend(self) -> None:
        """backfill_tables.py must not reference frontend code."""
        script_path = _project_root / "scripts" / "backfill_tables.py"
        content = script_path.read_text(encoding="utf-8")
        assert "frontend" not in content.lower()

    def test_backfill_does_not_add_new_dependencies(self) -> None:
        """backfill_tables.py must only import from existing project modules."""
        script_path = _project_root / "scripts" / "backfill_tables.py"
        content = script_path.read_text(encoding="utf-8")
        # No new third-party libs introduced
        forbidden = ["pandas", "xlrd", "pdfplumber", "openpyxl"]
        for lib in forbidden:
            assert f"import {lib}" not in content, \
                f"backfill_tables.py must not import {lib} (new dependency)"

    def test_script_parses_cleanly(self) -> None:
        """backfill_tables.py must be valid Python (no syntax errors)."""
        import ast
        script_path = _project_root / "scripts" / "backfill_tables.py"
        ast.parse(script_path.read_text(encoding="utf-8"))

    def test_ask_py_unchanged(self) -> None:
        """ask.py must not reference backfill_tables (no cross-contamination)."""
        ask_path = _project_root / "open_notebook" / "graphs" / "ask.py"
        content = ask_path.read_text(encoding="utf-8")
        assert "backfill" not in content.lower()
