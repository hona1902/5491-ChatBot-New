## Why

Phase 1 built the backend infrastructure for table-aware QA — extraction, persistence, chunking, exact lookup in `source_chat`, and grounding prompts. However, there is no way for users to see what tables were extracted, no backfill path for existing sources, no notebook-level (ask.py) table strategy, and several extraction gaps (HTML, XLSX duplicates, .xls, huge tables) that limit real-world reliability. Phase 2 closes these gaps to make table-aware QA production-ready.

## What Changes

- **New API endpoints** expose extracted table data: `GET /sources/{id}/tables` and `GET /sources/{id}/tables/{table_id}` (with row pagination); `GET /sources/{id}` gains a `table_count` field.
- **Frontend Table Preview UI** in Source Detail shows all extracted tables with page/sheet metadata, paginated rows, and copy-as-Markdown/CSV actions.
- **Backfill script** (`scripts/backfill_tables.py`) extracts tables and re-embeds rows for all existing sources that pre-date Phase 1.
- **ask.py table-aware strategy** adds a source-identification step before exact lookup so the notebook Q&A does not scan all source_table records blindly.
- **Table extraction improvements**: HTML table extraction; XLSX duplicate-header deduplication; decision on `.xls` handling (openpyxl does not support .xls — document the limitation or add xlrd).
- **Safety controls**: `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS` env var caps `source.tables_markdown`; per-table column and row hard caps prevent memory exhaustion on pathological files.
- **Improved semantic row retrieval**: upgrade beyond word-level keyword matching toward lightweight embedding-based similarity for `table_exact_lookup`.

## Capabilities

### New Capabilities
- `table-api-endpoints`: REST API for listing and paginating extracted tables per source, plus `table_count` on source detail.
- `table-preview-ui`: Frontend component in Source Detail that renders extracted tables with metadata, pagination, and copy actions.
- `table-backfill`: CLI script to extract tables and re-embed rows for pre-Phase-1 sources.
- `ask-table-strategy`: Notebook-level (ask.py) table-aware QA strategy that first identifies candidate CSV/XLSX sources before running exact lookup.
- `table-safety-controls`: Environment-variable-driven caps on `tables_markdown` size, column count, and row count to prevent OOM and context bloat.
- `table-extraction-v2`: Extended table extractor registry with HTML support, XLSX header deduplication, and documented .xls handling.
- `table-semantic-retrieval`: Embedding-based (not word-level) similarity for `table_exact_lookup` to improve recall and reduce false negatives.

### Modified Capabilities
- `table-extraction`: Add HTML extractor; add XLSX duplicate-header deduplication; document/handle .xls files. Row and column cap env vars plug into `ExtractedTable` construction.
- `table-exact-lookup`: Upgrade keyword matching to lightweight embedding similarity; scope now extends to ask.py via the `ask-table-strategy` orchestration layer.
- `table-preview-ui`: Phase 1 spec defined this capability as deferred. Phase 2 fully implements it.

## Impact

- **Backend:** `open_notebook/domain/source.py` (table_count field), `open_notebook/graphs/source_chat.py` (semantic lookup upgrade), `open_notebook/graphs/ask.py` (new source-identification step), `open_notebook/utils/table_extractor_registry.py` (HTML + XLSX fixes), `open_notebook/utils/table_lookup.py` (embedding similarity), `open_notebook/utils/chunking.py` (column cap).
- **API:** `open_notebook/api/sources.py` — two new routes + modified source detail schema.
- **Frontend:** `frontend/src/routes/sources/[id]/` — new `TablesPanel.svelte` component, updates to source detail page.
- **Scripts:** new `scripts/backfill_tables.py`.
- **Config:** new env vars `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS`, `OPEN_NOTEBOOK_TABLE_MAX_COLS`.
- **Dependencies:** possibly `xlrd` for .xls; no other new dependencies (embedding similarity reuses the existing embedding model).
- **No breaking changes** to existing `source_table`, `source_embedding`, or `source` schemas.
