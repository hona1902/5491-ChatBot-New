# Phase 1 — Backend Only (Safe, Minimal, Risk-Adjusted)

## 1. Database Schema

- [x] 1.1 Add `source_table` SurrealDB table definition to the init script with fields: `source` (Record<source>), `table_id`, `page_number`, `sheet_name`, `column_headers`, `row_data`, `markdown_repr`, `row_count`, `col_count`, `truncated`, `created`
- [x] 1.2 Add optional fields to `source_embedding` table definition (additive, no migration of existing records): `chunk_type`, `table_id`, `row_index`, `page_number`, `sheet_name`
- [x] 1.3 Add `tables_markdown: Optional[str]` field to the `source` SurrealDB table definition (separate from `full_text`; stores concatenated markdown_repr of all extracted tables)

## 2. Domain Model

- [x] 2.1 Create `SourceTable` Pydantic model in `open_notebook/domain/notebook.py` extending `ObjectModel` with `table_name = "source_table"` and all fields from spec (no `html_repr` field)
- [x] 2.2 Add `get_tables(source_id: str) -> List[SourceTable]` classmethod to `SourceTable` that queries `SELECT * FROM source_table WHERE source = $source_id`
- [x] 2.3 Update `Source` domain model to include `tables_markdown: Optional[str] = None` field
- [x] 2.4 Update `Source.delete()` to cascade-delete `source_table` records: run `DELETE source_table WHERE source = $source_id` before deleting the source record

## 3. Extraction Infrastructure

- [x] 3.1 Create `open_notebook/utils/table_extractor_registry.py` with `ExtractedTable` Pydantic model (fields: `table_id`, `source_id`, `page_number`, `sheet_name`, `column_headers`, `row_data`, `markdown_repr`, `row_count`, `col_count`, `truncated`) and `TABLE_MAX_ROWS = int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_ROWS", 5000))`
- [x] 3.2 Implement CSV extractor using **stdlib `csv.DictReader`** (no pandas): read up to `TABLE_MAX_ROWS` rows, set `sheet_name=None`, `page_number=None`; generate `markdown_repr` from headers + rows; set `truncated=True` if rows were dropped
- [x] 3.3 Implement XLSX extractor using **`openpyxl`**: iterate sheets via `workbook.worksheets`, treat first row as headers, each sheet → one `ExtractedTable` with `sheet_name` populated
- [x] 3.4 Implement PDF extractor using **`fitz`/PyMuPDF** (already imported in `pdf_table_preserver.py`): call `page.find_tables()` + `table.extract()` per page; convert 2D grid to `ExtractedTable` with `page_number`; catch exceptions per-page and log warnings without raising
- [x] 3.5 Extend `open_notebook/utils/docx_table_extractor.py` with `extract_docx_tables_structured() -> List[ExtractedTable]`; reuse existing XML traversal logic; preserve the existing `extract_docx_with_tables()` function unchanged for backward compatibility
- [x] 3.6 Add dispatch function `extract_tables_from_source(file_path: str, source_id: str) -> List[ExtractedTable]` that routes by file extension (`.csv`, `.xlsx`/`.xls`, `.pdf`, `.docx`); unknown extensions return `[]`; all extractors are wrapped in try/except to ensure no extraction failure can raise from the dispatcher

## 4. Ingestion Graph Update

- [x] 4.1 Add async `extract_tables` graph node in `open_notebook/graphs/source.py`: call `extract_tables_from_source(file_path, source_id)`, persist each `ExtractedTable` as a `source_table` record via `SourceTable(...).save()`, collect all `markdown_repr` values, concatenate into `tables_markdown` string
- [x] 4.2 Update `save_source` graph node to write `source.tables_markdown` (the concatenated table Markdown from step 4.1) to the source record; this is a separate field, NOT appended to `full_text`
- [x] 4.3 Wire `extract_tables` into the graph: `content_process → extract_tables → save_source`; all exceptions in `extract_tables` are caught, logged as warnings, and ingestion continues (non-blocking node)
- [x] 4.4 Update `_format_source_context()` in `graphs/source_chat.py` to append `source.tables_markdown` as a `## TABLE DATA` section after the prose section when `tables_markdown` is not None; this section is NOT subject to the 5,000-char prose truncation limit

## 5. Table-Aware Chunking

- [x] 5.1 Add `chunk_table(table: ExtractedTable) -> List[str]` function to `open_notebook/utils/chunking.py`; each returned string = `header_row + separator + one_data_row` in GFM Markdown; enforce relaxed token limit (`CHUNK_SIZE * 3`) per chunk; if a row exceeds limit, truncate cell values with `[truncated]` marker and log a warning
- [x] 5.2 Update `embed_source_command` in `commands/embedding_commands.py`: after deleting old embeddings, query `SELECT * FROM source_table WHERE source = $source_id`; if table records exist, call `chunk_table()` for each and collect table-row chunks alongside prose chunks; call `generate_embeddings()` on the full combined list in a single batch
- [x] 5.3 In `embed_source_command`, build **two separate record dicts** for `repo_insert`:
  - Prose record: `{"source": ..., "order": idx, "content": chunk, "embedding": embedding}` (no table fields)
  - Table-row record: `{"source": ..., "order": idx, "content": chunk, "embedding": embedding, "chunk_type": "table_row", "table_id": table_id, "row_index": row_idx, "page_number": page_number, "sheet_name": sheet_name}` — all five table fields must be present in the actual Python dict passed to `repo_insert`, not just documented

## 6. Table Exact Lookup (source_chat only)

- [x] 6.1 Create `open_notebook/utils/table_lookup.py` with `table_exact_lookup(query: str, source_id: str) -> Optional[str]`: fetch source_table records for source_id; guard on file extension (.csv/.xlsx only); tokenize query; scan row_data for case-insensitive cell value matches; return Markdown table of matching rows or None
- [x] 6.2 Integrate `table_exact_lookup` in `graphs/source_chat.py` as a pre-LLM context step: if source file is CSV/XLSX and `table_exact_lookup` returns a result, prepend a `## Verified Table Data` section to the system context; LLM still generates the final answer using this verified data

## 7. Prompt Updates

- [x] 7.1 Update `prompts/ask/query_process` template to add a table-grounding section: if retrieved chunks include `chunk_type = "table_row"` items, answer only from those rows; cite as `[Source: {title}, Table {table_id}, Page {page or N/A}, Sheet: {sheet or N/A}]`; if the specific value is not in the retrieved rows, state "not found in the available table data" rather than guessing
- [x] 7.2 Update `prompts/source_chat/system` template to conditionally include a table-grounding block when `## TABLE DATA` section is present in context; instruct the model to prefer verified table data over prose for exact-value questions; cite table source/page/sheet
- [x] 7.3 Update `prompts/ask/final_answer` template to preserve table citations verbatim from sub-answers in the final consolidated answer

## 8. API Endpoint

- [ ] 8.1 Add `GET /sources/{id}/tables` route to the backend API router; perform same RBAC ownership check as `GET /sources/{id}`; return JSON array of `SourceTableResponse` (without `row_data`) ordered by `page_number`, then `sheet_name`
- [ ] 8.2 Add `SourceTableResponse` Pydantic model to `api/models.py` with fields: `table_id`, `source_id`, `page_number`, `sheet_name`, `column_headers`, `row_count`, `col_count`, `truncated`, `markdown_repr`, `created`
- [ ] 8.3 Update `GET /sources/{id}` response to include `table_count: int` (count of source_table records for this source)

## 9. Tests

- [ ] 9.1 Create test fixtures in `tests/fixtures/`: `sample.csv` (5 rows, 3 cols with headers), `sample.xlsx` (2 sheets), `sample_with_table.docx`, `sample_with_table.pdf` (text-based, not scanned)
- [ ] 9.2 Write `tests/test_table_extractor_registry.py` — unit test each extractor (CSV, XLSX, DOCX, PDF) using fixture files; assert `column_headers`, `row_data`, `row_count`, `truncated`, `markdown_repr` contains the header row
- [x] 9.3 Write `tests/test_table_chunking.py` — unit tests for `chunk_table()`: assert header is present in every chunk, one data row per chunk, token limit respected, `[truncated]` marker present when a row is oversized
- [x] 9.4 Write `tests/test_table_lookup.py` — unit tests for `table_exact_lookup()`: exact match returns correct Markdown, no match returns None, case-insensitive matching works, non-CSV source returns None immediately
- [x] 9.5 Write `tests/test_embed_source_table_chunks.py` — unit test the dict-building logic in `embed_source_command`: mock `source_table` records with known data; assert the records list passed to `repo_insert` includes dicts with `chunk_type`, `table_id`, `row_index`, `page_number`, `sheet_name` for table rows and that prose records omit these fields

---

# Phase 2 — Deferred (separate change, do not implement now)

- [ ] P2.1 Frontend `SourceTableList` component — calls `GET /sources/{id}/tables`; collapsible list with page/sheet badges
- [ ] P2.2 Frontend `TablePreview` modal — calls `GET /sources/{id}/tables/{table_id}` with pagination
- [ ] P2.3 Chat citation badge UI — render `[Source: ..., Table ..., Page ..., Sheet: ...]` as inline chips
- [ ] P2.4 Migration script `scripts/migrate_extract_tables.py` — idempotent re-extraction for existing sources; skip if source_table records exist
- [ ] P2.5 Notebook-level exact lookup in `ask.py` — requires source-identification disambiguation layer (identify candidate CSV/XLSX sources from query before running exact lookup)
- [ ] P2.6 HTML table extractor for URL sources
- [ ] P2.7 `GET /sources/{id}/tables/{table_id}` detail endpoint with row pagination
- [ ] P2.8 Admin "Re-extract tables" button in Source Detail panel
- [ ] P2.9 `OPEN_NOTEBOOK_TABLE_MAX_COLS` env var for column width limiting
