## Context

Phase 1 and Phase 2 of table-aware QA are complete and released (v1.0.0). The system now:
- Extracts and persists structured tables (`source_table`) during ingestion for CSV, XLSX, DOCX, and PDF sources.
- Embeds table rows as `source_embedding` chunks with `chunk_type = "table_row"`.
- Provides `table_exact_lookup` with keyword + semantic fallback for source-level chat.
- Integrates `identify_table_source` and Verified Table Data injection into the `ask.py` notebook QA graph — but **only for CSV/XLSX sources**.

The current notebook QA graph (`ask.py`) follows this flow:

```
START → agent (strategy) → identify_table_source → fan-out(provide_answer) → write_final_answer → END
```

Evidence is gathered by `vector_search()` in `provide_answer`, which retrieves embedded chunks (insights + table rows) without awareness of what *kind* of evidence the question demands. This works well for overview/summary questions but fails for:
1. Factual/table questions where insight summaries paraphrase or omit cell values.
2. Legal/amendment questions where clause-level citations are mandatory.
3. Comparison questions where transformation outputs summarise rather than preserve original data.

**Key constraint**: The codebase has no concept of "source mode" as an explicit field. Insight-only behavior is simply the default: `Notebook.get_sources()` omits `full_text`, and transformations produce insight summaries. Full content is available on the `Source` record but never fetched during notebook QA.

## Goals / Non-Goals

**Goals:**
- G1: Add an evidence classification step to notebook QA that categorises questions into evidence tiers.
- G2: Automatically fall back to full source content and/or source_table when high-precision evidence is needed — without user intervention.
- G3: Expand table source eligibility beyond CSV/XLSX to any source with `source_table` records.
- G4: Update prompts to label four evidence tiers with clear conflict-resolution rules.
- G5: Ensure amendment/comparison questions cite original clauses, pages, and tables.
- G6: Surface evidence metadata (optional) so users know which evidence layer grounded the answer.
- G7: Maintain backward compatibility — no database migrations, no breaking API changes.
- G8: Keep summary-only questions fast (no unnecessary full-content fetch).

**Non-Goals:**
- NG1: Changing the ingestion pipeline or how `source_table` records are created.
- NG2: Adding a user-facing "source mode" toggle to the UI (this is an automatic internal mechanism).
- NG3: Modifying `source_chat.py` (single-source chat already has full-content context + table lookup).
- NG4: Real-time re-indexing or re-embedding of existing sources.
- NG5: Building a general-purpose RAG ranking framework — this is a targeted evidence routing layer.

## Decisions

### D1: Evidence Classification via LLM — Inline in Strategy Node

**Decision**: Extend the existing `Strategy` model returned by `call_model_with_messages` to include an `evidence_need` field. The strategy LLM already analyses the question; adding classification is marginal cost.

**Alternatives considered**:
- *Separate classification node*: Adds an extra LLM call and latency for every question. Rejected — the strategy LLM already has full question context.
- *Heuristic classifier (regex/keyword)*: Fragile for Vietnamese/bilingual Agribank content; misses nuanced queries. Rejected for primary use, but kept as a fast fallback if the LLM omits the field.

**Evidence tiers**:
| Tier | `evidence_need` value | Meaning |
|------|----------------------|---------|
| 1 | `overview` | Insight summaries are sufficient |
| 2 | `factual` | Full content and/or table data required |
| 3 | `legal_comparison` | Full content required; must cite clauses/pages/tables |

The heuristic fallback scans for keywords: table, column, row, clause, amendment, so sánh, điều khoản, etc.

### D2: Full-Content Fetch — On-Demand, Not Pre-Loaded

**Decision**: When `evidence_need ∈ {factual, legal_comparison}`, a new `fetch_evidence` helper queries `source.full_text` for sources linked to the notebook (scoped via `notebook_id`). Content is truncated to a configurable character limit (`EVIDENCE_FULL_TEXT_MAX_CHARS`, default 20,000) and injected as "Full Source Evidence" into the prompt.

**Alternatives considered**:
- *Always include full_text*: Massively increases prompt token cost for simple questions. Rejected.
- *Store full_text in vector DB*: Already done via chunked embeddings, but chunks lose table structure. Not sufficient alone.

**Performance safeguard**: For `evidence_need = overview`, the graph skips full-content fetch entirely — no additional DB query, no latency change.

### D3: Expand `identify_table_source` to All Source Types with `source_table`

**Decision**: Remove the `_source_is_tabular` filter (CSV/XLSX only) and replace it with a direct `source_table COUNT > 0` check. DOCX and PDF sources that have extracted tables already have `source_table` records; they should be eligible for `table_exact_lookup`.

**Alternatives considered**:
- *Keep CSV/XLSX filter and add separate DOCX/PDF path*: More code, same result. Rejected.

### D4: Evidence Hierarchy in Prompts — Static Rules, Not Dynamic

**Decision**: The conflict resolution order is hard-coded in prompt templates:
1. **Verified Table Data** (structured, cell-level)
2. **Full Source Evidence** (original prose with tables inline)
3. **Transformation Output** (summaries — guidance only, not canonical)
4. **Insight Summary** (lightweight, may omit details)

This avoids complex runtime arbitration; the LLM follows static instructions.

### D5: Transformer Output Guardrail — Prompt-Level, Not Code-Level

**Decision**: Add explicit instructions in `final_answer.jinja` stating that transformation outputs are summaries, not canonical evidence. The LLM must verify claims from transformation outputs against full_text/source_table before stating them as facts.

**Alternatives considered**:
- *Programmatically strip transformation content for legal questions*: Overly aggressive — transformation outputs can provide useful context even for high-precision questions.

### D6: Evidence Metadata — Optional Response Field

**Decision**: Add an optional `evidence_metadata` dict to the ask response containing `evidence_need`, `evidence_layers_used`, and `fallback_occurred`. This is informational only — the frontend can optionally render a badge or tooltip.

**Implementation**: Threaded through `ThreadState` → `write_final_answer` → API response.

### D7: Graph Topology — Minimal Change

**Decision**: The graph topology stays the same:
```
START → agent → identify_table_source → fan-out(provide_answer) → write_final_answer → END
```

The `evidence_need` field flows through `ThreadState`. The `provide_answer` node uses it to conditionally fetch full content. No new nodes are needed.

**Alternatives considered**:
- *Add `classify_evidence` as a separate node between `agent` and `identify_table_source`*: Adds complexity to the graph wiring for no functional benefit since the classification happens inside the strategy LLM.

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM misclassifies evidence need | Wrong tier → either missing evidence (classified too low) or wasted tokens (classified too high) | Heuristic fallback if LLM returns no `evidence_need`; conservative default to `factual` for ambiguous cases; keyword override for known patterns (table, amendment, clause) |
| Full-content fetch increases latency for factual questions | ~50-200ms additional DB round-trip | Only triggered when `evidence_need ≠ overview`; char limit caps the injected text; summary questions are unaffected |
| Prompt size increases for high-evidence questions | May exceed model context window | Hard cap `EVIDENCE_FULL_TEXT_MAX_CHARS` (default 20,000); truncation strategy preserves first/last sections for legal docs |
| Expanding table eligibility to DOCX/PDF increases `identify_table_source` DB queries | Marginal — one `COUNT` query per source | Already bounded by notebook source count (typically < 20) |
| Transformer guardrail too aggressive → useful context discarded | LLM may over-discount transformation output | Prompt wording is "verify against original" not "ignore transformation" |

## Migration Plan

1. **No database migration** — all evidence routing is code-level.
2. **Backward compatible** — `evidence_need` defaults to `overview` if absent from `Strategy`, so existing saved checkpoints still work.
3. **Rollback** — revert the code changes; no data cleanup needed.
4. **Config knobs** — all thresholds are environment variables with safe defaults.

## Open Questions

1. **Vietnamese keyword list for heuristic fallback**: Need a validated list of Vietnamese legal/table/comparison keywords for the fallback classifier. Can be refined after initial deployment.
2. **Full-text truncation strategy for legal documents**: Should we truncate from the middle (preserving beginning and end) or from the end? Legal documents often have amendment clauses at the end.
3. **Evidence metadata in SSE stream**: Should `evidence_metadata` be included in the final SSE message or as a separate metadata event? Current ask endpoint streams the final answer.
