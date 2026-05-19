## 1. Safety Controls (env vars + extraction caps)

- [x] 1.1 Add `OPEN_NOTEBOOK_TABLE_MAX_COLS` env var (default 100) to `open_notebook/config.py` alongside existing `OPEN_NOTEBOOK_TABLE_MAX_ROWS`
- [x] 1.2 Add `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS` env var (default 50000) to `open_notebook/config.py`
- [x] 1.3 Add `TABLE_LOOKUP_SIMILARITY_THRESHOLD` env var (default 0.4) and `ASK_TABLE_SOURCE_THRESHOLD` env var (default 0.35) to `open_notebook/config.py`
- [x] 1.4 Move row/column cap enforcement into `TableExtractorRegistry` base logic (after extraction, before returning) so all extractors share the same caps without duplication
- [x] 1.5 Add `original_headers: Optional[List[str]]` field to `ExtractedTable` Pydantic model for XLSX deduplication debugging
- [x] 1.6 Update `tables_markdown` assembly in the `save_source` graph node to truncate at table boundary when `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS` is exceeded; append truncation marker; log warning
- [x] 1.7 Write unit tests for column cap: table with 150 cols → 100 cols stored, `truncated=True`, warning logged
- [x] 1.8 Write unit tests for `tables_markdown` truncation: 6 tables fit, 7th pushed over cap → marker appended, 4 tables omitted
- [x] 1.9 Write unit test for row+column cap both triggering on same table

## 2. Table Extraction Improvements

- [x] 2.1 Add XLSX duplicate-header deduplication inside the XLSX extractor: detect duplicates, rename with `_2`/`_3` suffix, store `original_headers`, log warning
- [x] 2.2 Write unit test: XLSX sheet with `["Name","Score","Name","Score"]` headers → `["Name","Score","Name_2","Score_2"]`, `original_headers` set, warning logged
- [x] 2.2b Write unit test: after ingesting a DOCX/XLSX with duplicate headers, query `source_table` from SurrealDB and assert `original_headers` field is non-null (verifies the field is persisted in `repo_insert`, not just set on the Python model)
- [x] 2.3 Implement HTML table extractor in `open_notebook/utils/table_extractor_registry.py` using stdlib `html.parser`; register for `.html` and `.htm` extensions; handle nested tables by flattening
- [x] 2.4 Write unit test: HTML file with one table → one `ExtractedTable` with correct headers and rows
- [x] 2.5 Write unit test: HTML file with no `<table>` → empty list, no exception
- [x] 2.6 Write unit test: malformed HTML → empty list, warning logged
- [x] 2.7 Add `.xls` handling: check if `xlrd` is importable; if yes, delegate to xlrd extractor; if no, return `[]` with user-visible warning `"XLS files are not supported. Please convert to XLSX format."`
- [x] 2.8 Write unit test: `.xls` without xlrd → empty list, warning message matches spec
- [x] 2.9 Verify all existing Phase 1 extractor tests still pass (no regression on CSV, XLSX, PDF, DOCX)

## 3. Semantic Retrieval Upgrade (table_lookup.py)

- [x] 3.1 Refactor `table_exact_lookup` in `open_notebook/utils/table_lookup.py` into two phases: (a) keyword pre-filter, (b) embedding similarity fallback
- [x] 3.2 Implement Phase 2 fallback: embed query with configured embedding model, query `source_embedding WHERE source=$id AND chunk_type="table_row"`, compute cosine similarity, return top-N rows above `TABLE_LOOKUP_SIMILARITY_THRESHOLD`
- [x] 3.3 Write unit test: keyword match found → Phase 2 is never called
- [x] 3.4 Write unit test: keyword miss but semantically similar row → Phase 2 returns matching row
- [x] 3.5 Write unit test: both phases miss → `None` returned
- [x] 3.6 Write unit test: non-CSV/XLSX source → `None` returned immediately, no embedding call
- [x] 3.7 Verify existing `source_chat.py` integration tests still pass with updated `table_exact_lookup`

## 4. Table API Endpoints (backend)

- [x] 4.1 Add `SourceTableListItem` and `SourceTableDetailResponse` Pydantic response models to `api/models.py` (the existing models file — do NOT create a new `api/models/tables.py` subdirectory)
- [x] 4.2 Add `SourceTableDetailResponse` Pydantic model to `api/models.py` with `rows`, `total_rows`, `offset`, `limit`, and full table metadata fields
- [x] 4.3 Implement `GET /sources/{source_id}/tables` endpoint in `api/routers/sources.py`: query SurrealDB ordered by `page_number ASC NULLS LAST, sheet_name ASC NULLS LAST`, apply `Depends(get_current_user)` RBAC, return list of `SourceTableListItem`
- [x] 4.4 Implement `GET /sources/{source_id}/tables/{table_id}` endpoint in `api/routers/sources.py`: fetch single `source_table` record, apply `?offset` (default 0) and `?limit` (default 200, max 1000), return `SourceTableDetailResponse`
- [x] 4.5 Add `table_count: int` to `GET /sources/{source_id}` response: compute using an inline SurrealDB subquery `(SELECT VALUE count() FROM source_table WHERE source = $parent.id GROUP ALL)[0].count OR 0` inside the existing SELECT — do NOT add a separate DB round-trip (match the pattern used for `insights_count` at line 201 of `api/routers/sources.py`)
- [x] 4.6 Write API test: `GET /sources/{source_id}/tables` returns 200 with correct list for owner
- [x] 4.7 Write API test: `GET /sources/{source_id}/tables` returns 403 for non-owner
- [x] 4.8 Write API test: `GET /sources/{source_id}/tables` returns 200 `[]` for source with no tables
- [x] 4.9 Write API test: `GET /sources/{source_id}/tables/{table_id}` pagination: offset+limit respected, `total_rows` correct
- [x] 4.10 Write API test: `GET /sources/{source_id}/tables/nonexistent` returns 404
- [x] 4.11 Write API test: `GET /sources/{source_id}` response includes `table_count` field

## 5. ask.py Table-Aware Strategy

- [x] 5.1 Add `candidate_source_id: Optional[str]` field to `SubGraphState` (or equivalent state object) in `open_notebook/graphs/ask.py`
- [x] 5.2 Implement `identify_table_source` graph node: query notebook sources, filter to CSV/XLSX with `source_table` records, apply single-source fast path or embedding-ranked disambiguation
- [x] 5.3 Wire `identify_table_source` into ask.py graph before the retrieval step
- [ ] 5.4 Implement scoped `table_exact_lookup` call in ask.py using `candidate_source_id` (only when not None); prepend result as `## Verified Table Data` section
- [x] 5.5 Implement `ASK_TABLE_SOURCE_THRESHOLD` clamping and configurable threshold logic
- [x] 5.6 Write unit test: notebook with no CSV/XLSX → `candidate_source_id = None`, no embedding call, lookup skipped
- [x] 5.7 Write unit test: notebook with one CSV source → `candidate_source_id` set without embedding
- [x] 5.8 Write unit test: notebook with multiple CSV sources, query matches source 2 → source 2 selected
- [x] 5.9 Write unit test: multiple sources, all scores below threshold → `candidate_source_id = None`
- [ ] 5.10 Write integration test: end-to-end ask.py with a CSV source, exact-match query → answer grounded in table data

## 6. Frontend TablesPanel Component (React / Next.js)

> **Framework note:** The frontend is Next.js App Router with React and TypeScript (`.tsx`). There is NO Svelte in this project. All tasks in this section use React hooks and TypeScript.

- [x] 6.1 Add `table_count?: number` to `SourceDetailResponse` interface in `frontend/src/lib/types/api.ts`; add `SourceTableListItem` and `SourceTableDetailResponse` interfaces to the same file
- [x] 6.2 Add `getTables(sourceId: string)` and `getTableDetail(sourceId: string, tableId: string, offset: number, limit: number)` methods to `frontend/src/lib/api/sources.ts` (Axios client)
- [x] 6.3 Create `frontend/src/components/source/TablesPanel.tsx` as a React component (`.tsx`); accept `sourceId: string` and `tableCount: number` as props
- [x] 6.4 Implement table list view inside `TablesPanel.tsx`: render each table's index, `page_number`, `sheet_name`, `row_count`, `col_count`, and a "Truncated" badge using existing shadcn/ui `Badge` component
- [x] 6.5 Implement row pagination within each expanded table: use `useState` for current page, fetch rows via `getTableDetail` with `offset` and `limit=50`; show prev/next controls
- [x] 6.6 Implement "Copy as Markdown" button: call `getTableDetail` to get `markdown_repr`, copy to clipboard via `navigator.clipboard.writeText()`
- [x] 6.7 Implement "Copy as CSV" button: convert `rows` + `column_headers` to RFC 4180 CSV string (handle commas/quotes), copy via `navigator.clipboard.writeText()`
- [x] 6.8 Import `TablesPanel` inside `frontend/src/components/source/SourceDetailContent.tsx`; render it below the existing source detail sections; hide entirely when `table_count === 0` or `table_count` is undefined
- [x] 6.9 Add loading state (use existing spinner pattern from the project) and error state (inline error message) to `TablesPanel.tsx`
- [x] 6.10 Manual smoke test: open a CSV source in the UI → Tables panel visible with correct row count; open a PDF source with no tables → Tables panel hidden

## 7. Backfill Script

- [x] 7.1 Create `scripts/backfill_tables.py` with CLI argument parsing (`--source-ids`, `--notebook-id`, `--force`, `--dry-run`, `--skip-embeddings`, `--concurrency N` where default N=1); when `--skip-embeddings` is passed, print a visible WARNING after the run: `WARNING: --skip-embeddings was set. Semantic row retrieval will not work for backfilled sources until embeddings are generated. Run again without --skip-embeddings to complete.`
- [x] 7.2 Implement idempotency check: skip sources with existing `source_table` records unless `--force`
- [x] 7.3 Implement `--force` path: delete existing `source_table` and table-row `source_embedding` records before re-extracting
- [x] 7.4 Implement structured progress output: one line per source with `OK/SKIP/ERROR`, source ID, table count, row count
- [x] 7.5 Implement `--dry-run`: report what would be processed without writing to DB, then exit 0
- [x] 7.6 Implement extraction failure handling: catch per-source exceptions, log error, continue batch, exit non-zero if any failed
- [x] 7.7 Verify backfill respects `OPEN_NOTEBOOK_TABLE_MAX_ROWS`, `OPEN_NOTEBOOK_TABLE_MAX_COLS`, `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS` caps
- [x] 7.8 Write unit test: `--dry-run` prints plan and writes nothing to DB
- [x] 7.9 Write unit test: source with existing tables is skipped without `--force`; re-processed with `--force`
- [x] 7.10 Write unit test: one source fails extraction → error logged, other sources continue, exit code non-zero
- [x] 7.11 Manual test: run backfill on a notebook with 3 pre-Phase-1 sources; verify `source_table` records created and `source_embedding` table-row chunks present

## 8. Final Integration and Regression

- [ ] 8.1 Run full existing test suite; confirm zero regressions on Phase 1 functionality
- [ ] 8.2 Verify `source_chat` exact lookup still works correctly with the upgraded two-phase `table_exact_lookup`
- [ ] 8.3 Verify `source.tables_markdown` is still populated correctly for new ingestions with Phase 2 safety caps active
- [x] 8.4 Update `CHANGELOG.md` with Phase 2 changes
- [x] 8.5 Update `.env.example` with new env vars: `OPEN_NOTEBOOK_TABLE_MAX_COLS`, `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS`, `TABLE_LOOKUP_SIMILARITY_THRESHOLD`, `ASK_TABLE_SOURCE_THRESHOLD`
