# Table-Aware QA — Phase 1 Implementation Summary

**Change:** `table-aware-qa`
**Phase scope:** Backend only — no frontend changes
**Status:** Phase 1 COMPLETE ✅ — ready to merge
**Test run:** 2026-05-18 — 96 tests, 96 passed

---

## What Phase 1 Implemented

Phase 1 delivers end-to-end backend support for structured table data across ingestion,
embedding, and source-level question answering. No new Python dependencies were added;
all extractors use libraries already present in the project (`csv`, `openpyxl`, `fitz`,
`python-docx`).

### 1A — Table Extraction Infrastructure

- **`source_table` SurrealDB entity** stores one record per extracted table with full
  structural metadata: `source`, `table_id`, `page_number`, `sheet_name`,
  `column_headers`, `row_data`, `markdown_repr`, `row_count`, `col_count`, `truncated`.
- **`source_embedding` schema** extended with five optional metadata fields
  (`chunk_type`, `table_id`, `row_index`, `page_number`, `sheet_name`) — fully additive,
  existing records remain valid without migration.
- **`source.tables_markdown`** — new optional field on the `source` record; holds the
  concatenated GFM Markdown of all tables extracted from the source; kept separate from
  `full_text` so it is never subject to prose-truncation logic.
- **`SourceTable` domain model** added to `open_notebook/domain/notebook.py`:
  - `get_for_source(source_id)` classmethod
  - `Source.delete()` now cascade-deletes `source_table` records
- **`open_notebook/utils/table_extractor_registry.py`** — four format-specific extractors:

  | Format | Library           | Notes                                              |
  |--------|-------------------|----------------------------------------------------|
  | CSV    | `csv.DictReader`  | No pandas; stdlib only                             |
  | XLSX   | `openpyxl`        | Per-sheet tables; `sheet_name` populated           |
  | PDF    | `fitz`/PyMuPDF    | `page.find_tables()`; per-page errors swallowed    |
  | DOCX   | `python-docx` XML | `w:tbl` traversal; backward-compatible with existing `extract_docx_with_tables()` |

  Row cap controlled by `OPEN_NOTEBOOK_TABLE_MAX_ROWS` env var (default 5000).

- **`extract_tables` graph node** in `open_notebook/graphs/source.py` wired between
  `content_process → extract_tables → save_source`; all errors are caught and logged —
  ingestion always completes even when extraction fails.
- **`_format_source_context()`** in `graphs/source_chat.py` appends a `## TABLE DATA`
  section to LLM context from `source.tables_markdown`; this section is **not** subject
  to the 5,000-char `full_text` truncation limit.
- **Database migration 16** registers all schema additions; `migration_16_down` removes
  them cleanly.

### 1B — Table-Aware Chunking & Embedding

- **`chunk_table()`** in `open_notebook/utils/chunking.py` — one GFM Markdown chunk
  per data row (`header + separator + one_row`); relaxed token limit `CHUNK_SIZE × 3`
  per chunk; oversized cells truncated with `[truncated]` marker.
- **`embed_source_command`** in `commands/embedding_commands.py` — queries
  `source_table` records after deleting old embeddings, mixes table-row chunks with prose
  chunks in a single `generate_embeddings()` batch, and builds separate record dicts:
  - **Prose records:** `{source, order, content, embedding}`
  - **Table-row records:** `{source, order, content, embedding, chunk_type, table_id, row_index, page_number, sheet_name}`

### 1C — Deterministic Exact Lookup in `source_chat`

- **`open_notebook/utils/table_lookup.py`** — `table_exact_lookup(query, source_id)`:
  - Returns `None` immediately for non-CSV/XLSX sources (no row scan)
  - Fetches `source_table` records for the source
  - Tokenises the query (stop-word filtered)
  - Case-insensitive cell-value scan across all rows in all tables
  - Returns a GFM Markdown section of matched rows with `Table | Page | Sheet` labels,
    or `None` on no match
  - All exceptions swallowed — falls through to vector retrieval silently

- **`graphs/source_chat.py`** — pre-LLM exact-lookup step:
  - Extracts the **latest `HumanMessage`** from the message stack as the lookup query
    (`SystemMessage`, `AIMessage`, `ToolMessage`, `FunctionMessage` are all excluded)
  - Prepends `## Verified Table Data` to the formatted context when lookup returns a hit

- **Prompt updates:**
  - `prompts/ask/query_process.jinja` — table-grounding section: prefer
    `chunk_type=table_row` results; cite `[Source, Table, Page, Sheet]`; state
    "not found in the available table data" rather than hallucinating
  - `prompts/source_chat/system.jinja` — conditional table-grounding block when
    `## TABLE DATA` is present in context
  - `prompts/ask/final_answer.jinja` — preserve table citations verbatim in final answer

### Fix 3 — HumanMessage-only Lookup Query (applied during Phase 1C review)

The exact-lookup query in `source_chat.py` was tightened so that only genuine
`HumanMessage` instances are used as the lookup token source. `SystemMessage`,
`AIMessage`, `ToolMessage`, and `FunctionMessage` content is never passed as the
query string.

```python
# open_notebook/graphs/source_chat.py  lines 98–104
for msg in reversed(state.get("messages", [])):
    # Use only genuine HumanMessage so SystemMessage / AIMessage content
    # is never passed as the exact-lookup query.
    if isinstance(msg, HumanMessage):
        content = msg.content
        last_user_message = content if isinstance(content, str) else str(content)
        break
```

---

## Changed Files

| File | Type | Change |
|------|------|--------|
| `open_notebook/domain/notebook.py` | Modified | Added `SourceTable` model; `Source.tables_markdown` field; `Source.delete()` cascade |
| `open_notebook/utils/table_extractor_registry.py` | **New** | CSV / XLSX / PDF / DOCX extractors + `ExtractedTable` model |
| `open_notebook/utils/table_lookup.py` | **New** | Deterministic exact lookup utility |
| `open_notebook/utils/chunking.py` | Modified | Added `chunk_table()` function + `_TableProxy` helper |
| `open_notebook/graphs/source.py` | Modified | Added `extract_tables` node; wired into ingestion graph |
| `open_notebook/graphs/source_chat.py` | Modified | Phase 1C exact-lookup pre-step; `_format_source_context()` TABLE DATA section |
| `commands/embedding_commands.py` | Modified | Table-row chunks + full metadata dicts in `embed_source_command` |
| `prompts/ask/query_process.jinja` | Modified | Table-grounding section |
| `prompts/source_chat/system.jinja` | Modified | Conditional table-grounding block |
| `prompts/ask/final_answer.jinja` | Modified | Citation preservation |
| `data/migrations/migration_16.surql` | **New** | Schema: `source_table`, `source_embedding` fields, `source.tables_markdown` |
| `data/migrations/migration_16_down.surql` | **New** | Clean rollback |
| `tests/test_table_lookup.py` | **New** | 12 unit tests for `table_exact_lookup` |
| `tests/test_table_chunking.py` | **New** | 19 unit tests for `chunk_table` |
| `tests/test_embed_source_table_chunks.py` | **New** | 9 unit tests for embed record dict shapes |
| `tests/test_table_aware_qa_phase1a.py` | **New** | 56 integration/unit tests for Phase 1A |

**NOT modified:** `open_notebook/graphs/ask.py` (Phase 2 scope — confirmed clean)

---

## Schema Changes — Migration 16

### Added: `source_table` table

```surql
DEFINE TABLE source_table SCHEMAFULL;
DEFINE FIELD source         ON source_table TYPE record<source>;
DEFINE FIELD table_id       ON source_table TYPE string;
DEFINE FIELD page_number    ON source_table TYPE option<int>;
DEFINE FIELD sheet_name     ON source_table TYPE option<string>;
DEFINE FIELD column_headers ON source_table TYPE array<string>;
DEFINE FIELD row_data       ON source_table TYPE array<object>;
DEFINE FIELD markdown_repr  ON source_table TYPE string;
DEFINE FIELD row_count      ON source_table TYPE int;
DEFINE FIELD col_count      ON source_table TYPE int;
DEFINE FIELD truncated      ON source_table TYPE bool;
DEFINE FIELD created        ON source_table TYPE datetime DEFAULT time::now();
```

### Modified: `source_embedding` (additive optional fields)

```surql
DEFINE FIELD chunk_type  ON source_embedding TYPE option<string>;
DEFINE FIELD table_id    ON source_embedding TYPE option<string>;
DEFINE FIELD row_index   ON source_embedding TYPE option<int>;
DEFINE FIELD page_number ON source_embedding TYPE option<int>;
DEFINE FIELD sheet_name  ON source_embedding TYPE option<string>;
```

### Modified: `source` (additive optional field)

```surql
DEFINE FIELD tables_markdown ON source TYPE option<string>;
```

---

## Test Results

```
tests/test_table_lookup.py                12 passed   0.68 s
tests/test_table_chunking.py              19 passed
tests/test_embed_source_table_chunks.py    9 passed
tests/test_table_aware_qa_phase1a.py      56 passed   2.82 s
─────────────────────────────────────────────────────────────
Total                                     96 passed  ✅
```

All tests use mocks — no live database required.
One Pydantic V2 deprecation warning from upstream `surreal_commands` (class-based
`Config`); does not affect runtime.

---

## Known Limitations

1. **Existing sources are not back-filled.** Sources ingested before migration 16 have no
   `source_table` records and no `tables_markdown`. Exact lookup returns `None` for them.
   A back-fill script is deferred to Phase 2 (P2.4).

2. **PDF extractor requires text-based PDFs.** `fitz.page.find_tables()` extracts nothing
   from scanned/image-only PDFs. No error is raised; the result is an empty table list.

3. **`ask.py` (notebook-level QA) does not use exact lookup.** The lookup is only active
   in `source_chat` where the source is unambiguously known. Notebook-level QA requires
   source disambiguation logic before exact lookup can be applied — deferred to P2.5.

4. **HTML/URL sources are not extracted.** `<table>` elements in URL-ingested pages
   are not stored as `source_table` records. Deferred to P2.6.

5. **No column count cap.** Wide tables are stored and rendered in full.
   `OPEN_NOTEBOOK_TABLE_MAX_COLS` is a Phase 2 item (P2.9).

6. **No API endpoint for table inspection.** `GET /sources/{id}/tables` does not exist;
   `table_count` is not on the source list response. Deferred to tasks 8.1–8.3.

7. **No fixture-based extractor integration tests.** `test_table_extractor_registry.py`
   using dedicated fixture files (`sample.csv`, `sample.xlsx`, etc.) is deferred to
   tasks 9.1–9.2. Real PDF/DOCX files in `tests/` are used by existing tests already.

---

## Phase 2 Deferred Items

| ID   | Item |
|------|------|
| 8.1  | `GET /sources/{id}/tables` API route (RBAC, ordered by page/sheet) |
| 8.2  | `SourceTableResponse` Pydantic model (without `row_data`) |
| 8.3  | `table_count: int` on `GET /sources/{id}` response |
| 9.1  | Test fixtures: `sample.csv`, `sample.xlsx`, `sample_with_table.docx/pdf` |
| 9.2  | `test_table_extractor_registry.py` using real fixture files |
| P2.1 | Frontend `SourceTableList` component |
| P2.2 | Frontend `TablePreview` modal with row pagination |
| P2.3 | Chat citation badge UI (inline chips) |
| P2.4 | `scripts/migrate_extract_tables.py` — idempotent back-fill for existing sources |
| P2.5 | Notebook-level exact lookup in `ask.py` (requires source disambiguation) |
| P2.6 | HTML table extractor for URL sources |
| P2.7 | `GET /sources/{id}/tables/{table_id}` detail endpoint |
| P2.8 | Admin "Re-extract tables" button in Source Detail panel |
| P2.9 | `OPEN_NOTEBOOK_TABLE_MAX_COLS` env var |

---

## Manual QA Steps

Use these steps to verify the Phase 1 feature end-to-end in a running instance.

### Prerequisites
1. Ensure migration 16 has run: `POST /api/migrate` or start the app fresh.
2. Prepare a CSV file (e.g. `scores.csv`) with at least 5 rows and 3 named columns.
3. Optionally prepare an XLSX file with 2 sheets.

### Ingestion — CSV
1. Upload the CSV file as a source in any notebook.
2. Wait for processing to complete (source status = "completed").
3. **Verify in SurrealDB:**
   ```surql
   SELECT * FROM source_table WHERE source = source:<id>;
   ```
   Expect ≥1 record with `column_headers`, `row_data`, and `markdown_repr` populated.
4. **Verify `tables_markdown`:**
   ```surql
   SELECT tables_markdown FROM source WHERE id = source:<id>;
   ```
   Should be a non-null GFM Markdown string containing the table.

### Source Chat — Exact Lookup Hit
1. Open the source and start a source chat.
2. Ask a question naming a specific value from your data, e.g.:
   - "What is Alice's score?"
   - "Show me the row for Bob."
3. Verify the response contains the correct cell value from the CSV row.
4. Confirm server logs contain:
   ```
   table_exact_lookup: found N matching row(s) across M table(s) for source source:<id>
   ```

### Source Chat — No Match
1. Ask about a value that does **not** exist in the data (e.g. "What is Diana's score?").
2. The response should state the value is not found, not hallucinate from other rows.

### Source Chat — Non-CSV Source
1. Upload a plain text or image-only PDF source.
2. Start source chat and ask any question.
3. Confirm no `## Verified Table Data` header appears in the response context.
   Server logs should show:
   ```
   table_exact_lookup: source <id> is not CSV/XLSX, skipping
   ```

### XLSX Multi-Sheet
1. Upload an XLSX file with 2 sheets.
2. Confirm 2 `source_table` records exist (one per sheet).
3. Ask a question referencing a value on the second sheet.
4. Verify the response cites the correct sheet name.

---

## Rollback Notes — Migration 16

### What the down migration does

```surql
-- migration_16_down.surql
REMOVE TABLE source_table;
REMOVE FIELD chunk_type   ON source_embedding;
REMOVE FIELD table_id     ON source_embedding;
REMOVE FIELD row_index    ON source_embedding;
REMOVE FIELD page_number  ON source_embedding;
REMOVE FIELD sheet_name   ON source_embedding;
REMOVE FIELD tables_markdown ON source;
```

Or via the API (if registered): `POST /api/migrate/down`.

### What is affected

| Data | Effect |
|------|--------|
| All `source_table` records | **Deleted** — structured table data is lost |
| Optional fields on `source_embedding` | Removed from schema; existing prose records unaffected |
| `source.tables_markdown` | Removed from all source records |
| `source.full_text` | **Not touched** |
| Prose embeddings (`source`, `order`, `content`, `embedding`) | **Not touched** |

### Code to revert after DB rollback

After running the down migration, revert these files to their pre-Phase-1 state:

- `open_notebook/graphs/source_chat.py` — remove the `table_exact_lookup` import and
  the pre-LLM exact-lookup block (lines 91–123)
- `open_notebook/graphs/source.py` — remove `extract_tables` node and graph edge
- `commands/embedding_commands.py` — remove table-chunk logic from `embed_source_command`
- `open_notebook/utils/table_lookup.py` — delete file
- `open_notebook/utils/table_extractor_registry.py` — delete file

**Rollback is data-safe:** `full_text` and prose embeddings are fully preserved.
Re-ingesting sources after rollback restores them to their pre-Phase-1 state.
