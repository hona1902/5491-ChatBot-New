## Why

Open Notebook answers factual table questions incorrectly because tables in PDFs, DOCX, CSV, and XLSX files are flattened into plain-text chunks — row/column relationships are lost, headers are separated from data rows during chunking, and CSV/XLSX files receive no structured treatment at all. Users asking "What is the value in column X for row Y?" get hallucinated or wrong answers too often.

## What Changes

- **New ingestion extractors** for CSV and XLSX that read structured data directly instead of treating them as plain text.
- **New `source_table` database table** that stores every extracted table with rich metadata (source_id, table_id, page number, sheet name, column headers, row data, original markdown).
- **Table-aware chunking** that keeps the header row attached to every data-row chunk and never splits mid-table.
- **Table-aware retrieval** in the `ask` graph that queries `source_table` records alongside normal `source_embedding` results, always returning complete rows with headers.
- **Exact-lookup path** for CSV/XLSX queries that applies deterministic column/row filtering before falling back to LLM generation.
- **Updated prompt templates** (`ask/query_process`, `source_chat/system`) that instruct the model to answer only from retrieved table rows and cite source/table/page/sheet.
- **API endpoints** to expose extracted tables per source so the frontend can preview them.
- **Frontend `Source Detail` panel** extended to list and render extracted tables with source/page/sheet badges.
- **Migration script** to re-extract tables from already-ingested sources.

## Capabilities

### New Capabilities

- `table-extraction`: Detect and extract structured tables from PDF, DOCX, HTML, CSV, and XLSX during ingestion; store each table as a `source_table` record with full metadata.
- `table-chunking`: Chunk strategy that keeps headers attached to data rows and never splits across table boundaries; stored as `source_embedding` records with `chunk_type = "table_row"`.
- `table-retrieval`: Retrieval layer that fetches complete `source_table` rows (with headers) when a query is table-oriented, bypassing random text-chunk embedding search.
- `table-qa-prompts`: Updated system prompts that restrict answers to retrieved table data, enforce citation (source/table/page/sheet), and prohibit cell hallucination.
- `table-exact-lookup`: Optional deterministic row/column filter for CSV/XLSX before LLM generation.
- `table-preview-ui`: Frontend components to list, browse, and preview extracted tables inside Source Detail; display table/page/sheet citations in chat answers.

### Modified Capabilities

- `source-rbac`: Table records (`source_table`) must respect the same owner/notebook access control as `source` records — no requirement change to the RBAC rules themselves, but a new resource type must be protected.

## Impact

- **Backend**: `open_notebook/utils/` — new extractors for CSV, XLSX; updated `chunking.py` for table-aware splitting; new `table_extractor_registry.py`.
- **Domain**: `open_notebook/domain/notebook.py` — new `SourceTable` model, updated `Source.delete()` to cascade-delete `source_table` records, new `Source.get_tables()` method.
- **Database**: New SurrealDB `source_table` table with vector index; migration script in `scripts/`.
- **Graphs**: `graphs/source.py` — new `extract_tables` node after `content_process`; `graphs/ask.py` — hybrid retrieval node; `graphs/source_chat.py` — table context injection.
- **Prompts**: `prompts/ask/query_process`, `prompts/source_chat/system` — table citation and grounding rules.
- **API**: New routes in `api/` — `GET /sources/{id}/tables`, `GET /sources/{id}/tables/{table_id}`; updated source detail schema.
- **Frontend**: `frontend/` — `SourceTableList` component, `TablePreview` modal, citation badge in chat messages.
- **Dependencies**: `openpyxl` (XLSX), `pdfplumber` (PDF table extraction), `pandas` (CSV/XLSX row filtering) — likely already available or lightweight additions to `pyproject.toml`.
- **Tests**: New fixture files in `tests/fixtures/` (CSV, XLSX, PDF-with-table, DOCX-with-table, HTML-with-table); new test modules for extractors, chunking, retrieval, and prompts.
