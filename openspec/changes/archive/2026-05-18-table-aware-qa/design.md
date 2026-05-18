## Context

Open Notebook currently has two extraction utilities (`docx_table_extractor.py`, `pdf_table_preserver.py`) that produce Markdown table syntax, but those tables are still passed through the generic `chunk_text()` function which splits on character/token boundaries — it has no concept of table rows. A 10-row Markdown table becomes 3–4 text chunks; chunk boundaries arbitrarily fall inside the table, separating headers from data rows. Vector search then retrieves random slices. For CSV and XLSX files, content-core converts them to plain text with no row/column awareness.

**Architecture review findings** identified four critical bugs in the original design:
1. The `embed_source_command` insert dict schema was underspecified — table metadata fields were named in prose but never added to the actual `repo_insert` call.
2. Appending table Markdown to the tail of `source.full_text` with a `<!-- TABLES -->` marker was unsafe because `_format_source_context()` in `source_chat.py` hard-truncates `full_text` at 5,000 characters, silently discarding all trailing table content.
3. The exact-lookup feature was scoped to `ask.py` which has no `source_id` in its graph state — architecturally impossible without a complete redesign of the notebook-level graph.
4. `pdfplumber` was specified as a new dependency when `fitz`/PyMuPDF (already imported in `pdf_table_preserver.py`) provides the identical `page.find_tables()` + `table.extract()` API.

This revised design corrects all four issues and simplifies the scope to a safe Phase 1 backend implementation.

**Constraints (unchanged):**
- Must not break existing non-table sources (prose PDFs, URLs, audio).
- Must integrate with the existing SurrealDB + surreal-commands job architecture.
- Must respect RBAC: `source_table` records must not be accessible to users who cannot read the parent `source`.
- No new heavy dependencies in Phase 1 (`pandas`, `pdfplumber` — deferred or removed).

## Goals / Non-Goals

**Goals (Phase 1 — Backend only):**
- Store every detected table as a `source_table` SurrealDB record with: `source`, `table_id`, `page_number`, `sheet_name`, `column_headers`, `row_data`, `markdown_repr`, `row_count`, `col_count`, `truncated`, `created`.
- Add a `tables_markdown: Optional[str]` field to the `source` SurrealDB record, storing a concatenation of all extracted tables' `markdown_repr` values, **separate from `full_text`**. This field is used by `_format_source_context()` independently of the prose content and cannot be truncated by the 5,000-char prose limit.
- Chunk tables row-by-row with `chunk_table()`; store each row chunk in `source_embedding` with the metadata fields `chunk_type`, `table_id`, `row_index`, `page_number`, and `sheet_name` explicitly set in the `repo_insert` dict (not just documented — actually in the dict passed to `repo_insert`).
- Provide exact-lookup in `source_chat.py` only (where `source_id` is known state) for CSV/XLSX sources.
- Update prompt templates for table grounding and citation.
- Expose `GET /sources/{id}/tables` API endpoint.
- Write tests for extractors, chunking, and the embed_source command.

**Non-Goals (Phase 1):**
- Frontend table preview components — Phase 2.
- Notebook-level (`ask.py`) exact lookup — Phase 2 (requires `source_id` disambiguation layer first).
- Migration script for existing sources — Phase 2 (safe to re-ingest, no data loss risk).
- HTML table extractor for URL sources — Phase 2 (content-core already converts `<table>` to Markdown).
- OCR-based table extraction from scanned image PDFs.
- Table editing UI or cross-source table joins.

## Decisions

### D1 — New `source_table` SurrealDB table (unchanged from review)

**Decision:** Tables stored as separate `source_table` records linked to `source` via a `source` field, matching the pattern of `source_embedding` and `source_insight`.

**Rationale:** Structured access needed (fetch rows by source, filter by sheet). Embedding inside `full_text` reduces them to text.

---

### D2 — `tables_markdown` as a dedicated field on `source`, NOT appended to `full_text`

**Decision (revised from original):** Instead of appending table Markdown to the tail of `source.full_text` with a `<!-- TABLES -->` marker, table Markdown SHALL be stored in a new optional field `source.tables_markdown: Optional[str]`.

**Rationale:** `_format_source_context()` in `source_chat.py` truncates `full_text` to 5,000 characters:
```python
if len(full_text) > 5000:
    full_text = full_text[:5000] + "...\n[Content truncated]"
```
Any table Markdown appended after a prose section exceeding 5,000 chars would be silently discarded. The `<!-- TABLES -->` marker would never reach the LLM. By using a separate `tables_markdown` field, `_format_source_context()` can include it unconditionally after the prose section — the two fields are independent and neither truncates the other.

**Impact:** `_format_source_context()` must be updated to append a `## TABLE DATA` section using `source.tables_markdown` when it is present. The embed_source command must NOT use the `<!-- TABLES -->` marker for routing table chunks — instead it reads `source_table` records directly.

**Alternatives considered:**
- `<!-- TABLES -->` marker in `full_text`: Rejected — silently broken by 5,000-char truncation.
- Separate `source_insight` record of type "tables": Rejected — insight rendering assumes prose; breaks insight panel.
- Store Markdown inside `source_table.markdown_repr` only: Correct for storage but not accessible to `_format_source_context()` without an additional DB query. The `tables_markdown` field caches the concatenated result at save time.

---

### D3 — Extraction registry per file type (revised: no pdfplumber, no pandas)

**Decision:** Registry maps extension/MIME type to extractor. Phase 1 extractors use only dependencies already present:

| File type | Extractor | Dependency |
|-----------|-----------|-----------|
| `.csv` | stdlib `csv.DictReader` | stdlib only |
| `.xlsx` / `.xls` | `openpyxl` per-sheet | already transitive |
| `.docx` | Extend `docx_table_extractor.py` | already present |
| `.pdf` | `fitz`/PyMuPDF `page.find_tables()` + `table.extract()` | already present in `pdf_table_preserver.py` |
| `.html` | *(deferred to Phase 2)* | — |

**Why not `pdfplumber`?** `pdf_table_preserver.py` already imports `fitz` and calls `page.find_tables()` / `table.extract()`. This is the same API surface as `pdfplumber.page.extract_tables()`. Adding a second PDF library with overlapping capability is unjustified.

**Why not `pandas`?** stdlib `csv.DictReader` reads CSV into row dicts with headers, which is all Phase 1 needs. `pandas` adds ~30MB Docker image bloat for no additional feature. If aggregation is needed in Phase 2, it can be added then.

---

### D4 — Exact-lookup scoped to `source_chat.py` only (Phase 1)

**Decision (revised from original):** The deterministic row/column lookup for CSV/XLSX is implemented as a pre-LLM step in `source_chat.py` only, where `source_id` is a known field in `SourceChatState`. It is **not** implemented in `ask.py` in Phase 1.

**Rationale:** `ask.py`'s `SubGraphState` has no `source_id`. The graph searches across all notebooks using `vector_search()` without a source filter. Exact-lookup at the notebook level would require: (1) identifying which sources are CSV/XLSX, (2) disambiguating which source the query targets. This is a Phase 2 feature requiring a source-identification layer before the lookup.

**Phase 2 design note:** Notebook-level exact lookup should first run a lightweight source classifier that identifies candidate CSV/XLSX sources by topic/title match against the query, then run `table_exact_lookup` against those sources. This prevents cross-source false matches.

**Phase 1 flow in `source_chat.py`:**
1. Load `source` by `source_id` from state.
2. Check if `source.asset.file_path` ends with `.csv` or `.xlsx`.
3. If yes: call `table_exact_lookup(query=last_message, source_id=source_id)`.
4. If a result is returned, prepend it to the system context as a "Verified Table Data" section before normal LLM generation (not short-circuiting the LLM entirely — the LLM still formulates the response, but the verified data is pinned in context).
5. If no result, proceed normally with vector retrieval.

---

### D5 — Explicit `repo_insert` dict schema for table-row embeddings

**Decision:** The `embed_source_command` in `commands/embedding_commands.py` currently builds insert records as:
```python
{"source": ..., "order": idx, "content": chunk, "embedding": embedding}
```
Table-row chunks MUST include additional fields in this same dict at insert time:
```python
{
    "source": ...,
    "order": idx,
    "content": chunk,
    "embedding": embedding,
    "chunk_type": "table_row",      # Only for table chunks
    "table_id": table_id,           # string: the source_table record's table_id
    "row_index": row_idx,           # int: 0-based data row index
    "page_number": page_number,     # Optional[int]
    "sheet_name": sheet_name,       # Optional[str]
}
```
Prose chunks omit all optional fields (or pass `None` — SurrealDB stores `none`).

**Rationale:** The original design documented these fields in the spec but never explicitly specified they must appear in the `repo_insert` dict. Without this, the metadata is computed but discarded.

---

### D6 — No `source_table` vector index (revised from original)

**Decision:** No vector index is created on `source_table`. Semantic retrieval of table content is handled entirely through `source_embedding` records with `chunk_type = "table_row"`. The existing `fn::vector_search` SurrealDB function is used without modification.

**Rationale:** Redundant. Each table row is already individually embedded as a `source_embedding` row chunk. Adding a second vector index on `source_table.markdown_repr` would double the embedding work for tables and create a separate retrieval path that complicates ranking.

---

### D7 — API access control via parent source ownership check (unchanged)

`GET /sources/{id}/tables` uses the same ownership gate as `GET /sources/{id}`. No new RBAC resource type.

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| `fitz.page.find_tables()` misses tables in complex PDF layouts | Non-failing: if extraction returns empty list, source ingests without table records; existing `pdf_table_preserver.py` still fills `full_text` with Markdown tables |
| `tables_markdown` field grows large for XLSX with many sheets | Apply same `OPEN_NOTEBOOK_TABLE_MAX_ROWS` limit per table; total `tables_markdown` size is bounded |
| Large XLSX files slow ingestion | Row limit per table (env var `OPEN_NOTEBOOK_TABLE_MAX_ROWS`, default 5000) |
| `embed_source_command` is retried on failure — table metadata must be idempotent | The command deletes all `source_embedding` records before inserting; re-running always produces a consistent final state |
| Exact-lookup keyword matching produces false positives (e.g., "Alice" matches unrelated columns) | Matching is conservative: only returns rows where the looked-up value appears in a cell — the LLM still formulates the final answer and can discard spurious matches |
| `source.tables_markdown` may not be populated for sources ingested before this change | Phase 2 migration script handles re-extraction for existing sources |

## Migration Plan

**Phase 1 (this change):**
1. Add `source_table` table definition to SurrealDB init script.
2. Add optional fields `chunk_type`, `table_id`, `row_index`, `page_number`, `sheet_name` to `source_embedding` table definition.
3. Add `tables_markdown` field to `source` table definition.
4. Deploy backend code (extractors, graph node, chunking update, embed_source update, source_chat update, prompts, API endpoint).
5. Verify: ingest one CSV and one DOCX-with-table; confirm `source_table` records exist; confirm `source_embedding` records have `chunk_type = "table_row"`; confirm `source.tables_markdown` is populated; confirm source chat cites table correctly.

**Rollback:** `source_table` table can be dropped without affecting any other table. `source_embedding` optional fields are ignored by existing queries. `source.tables_markdown` being absent is handled as `None` in `_format_source_context()`.

**Phase 2 (separate change):**
- Migration script for existing sources (re-run `extract_tables` per source).
- Frontend `SourceTableList` + `TablePreview` components.
- Notebook-level exact lookup in `ask.py` (requires source-identification layer).
- HTML table extractor for URL sources.
- Admin "Re-extract tables" button.

## Open Questions

- **Q1 (resolved):** Exact-lookup in `ask.py` — deferred to Phase 2. Phase 1 is `source_chat.py` only.
- **Q2 (resolved):** `pdfplumber` — removed; use `fitz` already present.
- **Q3 (resolved):** `<!-- TABLES -->` marker — replaced by `source.tables_markdown` field.
- **Q4:** Should `tables_markdown` be included in the text indexed for full-text search (SurrealDB `fn::text_search`)? Recommendation: yes, add it to the full-text search index alongside `full_text`.
- **Q5:** Row limit default of 5000 — is this too large for XLSX sources that might have 500 columns? Recommendation: also add a `OPEN_NOTEBOOK_TABLE_MAX_COLS` env var (default 100) to limit column-width.
