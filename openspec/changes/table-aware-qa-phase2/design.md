## Context

Phase 1 delivered the full backend infrastructure: `source_table` persistence, `source_embedding` table-row chunks, `table_exact_lookup` in `source_chat`, and grounding prompts. The system works end-to-end for single-source chat on CSV/XLSX files. However, three production gaps remain:

1. **No visibility**: users cannot see what tables were extracted; there are no API endpoints or UI.
2. **No backfill**: sources ingested before Phase 1 have no `source_table` records or table-row embeddings.
3. **No notebook-level table strategy**: `ask.py` runs vector search blindly across all sources; table-exact lookup was explicitly excluded from Phase 1 scope.
4. **Extraction fragility**: HTML tables are unsupported; XLSX duplicate headers cause silent data loss; `.xls` files silently produce no tables; very wide/tall tables can cause OOM or context bloat.
5. **Weak semantic retrieval**: word-level keyword matching in `table_exact_lookup` produces false negatives for synonym queries and fuzzy values.

**Constraints:**
- No new heavy dependencies beyond possibly `xlrd` (for `.xls`).
- All changes must be backward-compatible: existing `source_table` and `source_embedding` records are valid as-is.
- Backfill must be safe to re-run (idempotent).
- ask.py changes must not degrade performance for prose-only notebooks.

## Goals / Non-Goals

**Goals:**
- Expose `GET /sources/{id}/tables` and `GET /sources/{id}/tables/{table_id}` with row pagination.
- Add `table_count` to `GET /sources/{id}` response.
- Build `TablesPanel.svelte` in Source Detail with metadata display, row pagination, and copy-as-Markdown/CSV.
- Provide a safe, idempotent `scripts/backfill_tables.py` CLI.
- Add a source-identification step in `ask.py` before running exact lookup (scoped to top-ranked CSV/XLSX source only).
- Add `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS` and `OPEN_NOTEBOOK_TABLE_MAX_COLS` env vars.
- Upgrade `table_exact_lookup` from keyword matching to lightweight cosine similarity on pre-computed embeddings.
- Add HTML table extractor; deduplicate XLSX headers; document `.xls` limitation.

**Non-Goals:**
- Real-time table editing or in-app table manipulation.
- Table diff / version history.
- Cross-notebook table search.
- Full-text search inside table cells (beyond what vector retrieval already provides).
- Automatic re-ingestion of existing sources (backfill is opt-in via CLI).
- Supporting `.xls` in Phase 2 if `xlrd` introduces licensing/security concerns (decision gate in implementation).

## Decisions

### D1: API design — list vs. detail endpoints
**Decision:** Implement both `GET /sources/{id}/tables` (list, no `row_data`) and `GET /sources/{id}/tables/{table_id}` (detail with `row_data`, paginated). The list endpoint omits `row_data` to keep payloads small; the detail endpoint returns rows with `?offset` and `?limit` (default 200).

**Alternative considered:** Single endpoint returning everything. Rejected because large tables (5000 rows × 20 cols) would make the list call unacceptably slow and memory-heavy.

### D2: Frontend table rendering — Markdown vs. native HTML table
**Decision:** Use native HTML `<table>` with virtual/paginated rendering in `TablesPanel.svelte`. Do NOT render the raw `markdown_repr` through a Markdown renderer — Markdown tables with hundreds of rows are slow to render and impossible to paginate.

**Alternative considered:** Render `markdown_repr` via marked.js. Rejected: no row pagination possible; very long tables freeze the browser.

### D3: Semantic retrieval upgrade — embedding-based similarity
**Decision:** Store a per-query embedding (using the same embedding model already configured) and compute cosine similarity against `source_embedding` content vectors already in SurrealDB, filtered to `chunk_type = "table_row"` for the candidate source. This reuses infrastructure already present.

**Alternative considered:** Use a third-party fuzzy matching library (e.g., `rapidfuzz`). Rejected: adds a dependency and still relies on string similarity, not semantic similarity. The existing embedding model is already loaded in process.

**Alternative considered:** Call a lightweight cross-encoder re-ranker. Rejected: too slow for interactive chat; overkill for row retrieval.

### D4: ask.py source identification — heuristic vs. embedding
**Decision:** Two-step: (1) filter sources in the current notebook to those with `.csv` or `.xlsx` extensions AND at least one `source_table` record. (2) If exactly one such source exists, use it. If multiple exist, rank by cosine similarity of query embedding to each source's title + topic field. Take the top-ranked source only if its score exceeds a configurable threshold (`ASK_TABLE_SOURCE_THRESHOLD`, default 0.35).

**Alternative considered:** Ask the LLM to identify the relevant source first. Rejected: adds an extra LLM call latency for every ask.py invocation even when there are no table sources.

### D5: Backfill safety — idempotency mechanism
**Decision:** Before extracting tables for a source, check if `source_table WHERE source = $id` already has records. If yes, skip unless `--force` flag is passed. This prevents double-extraction without requiring a migration flag column.

**Alternative considered:** Add a `backfilled_at` column to the `source` schema. Rejected: schema change is unnecessary overhead; the existing `source_table` presence check is sufficient.

### D6: Safety caps — hard caps vs. soft truncation
**Decision:** Apply hard caps at two levels: (a) `OPEN_NOTEBOOK_TABLE_MAX_ROWS` (existing, Phase 1) caps rows during extraction; (b) `OPEN_NOTEBOOK_TABLE_MAX_COLS` (new, default 100) caps columns during extraction; (c) `OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS` (new, default 50000) caps the concatenated `tables_markdown` written to the source record. Truncation is logged as a warning; `source.tables_markdown` gets a trailing `<!-- tables_markdown truncated -->` marker.

**Alternative considered:** Reject ingestion if caps are exceeded. Rejected: too disruptive; silent truncation with a warning is a better user experience.

## Risks / Trade-offs

- **[Risk] Backfill re-embeds rows, doubling embedding API cost for large sources** → Mitigation: `--dry-run` flag shows estimated cost before committing; `--source-ids` flag lets admins backfill selectively.
- **[Risk] ask.py source-identification step adds latency (~50ms for embedding + cosine)** → Mitigation: step is skipped entirely if the notebook has no CSV/XLSX sources (fast path via metadata check before embedding).
- **[Risk] HTML table extractor quality is lower than pdfplumber/openpyxl** → Mitigation: HTML extractor uses stdlib `html.parser`, no new dep; extraction failures log a warning and return empty list, consistent with Phase 1 error handling.
- **[Risk] XLSX duplicate header deduplication may silently rename columns (e.g., "Name" → "Name_2")** → Mitigation: log a warning with original and renamed headers; store `original_headers` in `source_table` record as an optional field for debugging.
- **[Risk] .xls support via `xlrd` adds an unmaintained dependency** → Mitigation: decision gate — if `xlrd`'s last release (2021) is considered acceptable, add it; otherwise document that `.xls` is unsupported and users must convert to `.xlsx`.
- **[Risk] Cosine similarity upgrade to `table_exact_lookup` may have higher latency than keyword scan** → Mitigation: keyword scan is kept as a fast pre-filter; embedding similarity is only invoked if keyword scan returns zero matches.
