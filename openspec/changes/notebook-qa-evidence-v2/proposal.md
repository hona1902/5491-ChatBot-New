## Why

Notebook-level Q&A (`ask.py` graph) currently retrieves evidence from vector search over embedded chunks — which are predominantly **insight summaries** produced by transformations. For Agribank legal/regulatory documents containing tables, amendment clauses, and comparison requirements, these insight summaries omit or distort table data and clause-level details, producing wrong answers. Switching the same source to "full content" mode consistently yields more accurate results. The table-aware QA Phase 1 and Phase 2 work has given us the building blocks (source_table persistence, table row embeddings, table_exact_lookup, TablesPanel), but notebook QA still has no mechanism to automatically fall back to full content or source_table evidence when the question demands it.

## What Changes

- **Add an evidence routing layer** in the `ask.py` graph that classifies each question's evidence need (overview → insights OK; factual/table/legal → full content + source_table required; amendment/comparison → full content + clause/page citations required).
- **Extend source mode semantics**: "insight-only" controls the default display/retrieval preference, not a hard gate that prevents access to verified evidence when the question demands it.
- **Expand table source eligibility** from CSV/XLSX-only to any source type that has `source_table` records (DOCX/PDF tables extracted during ingestion are already persisted but currently ignored by notebook QA).
- **Add a full-content fallback** path: when the evidence classifier determines high-precision evidence is needed, the graph fetches `source.full_text` for relevant sources and injects it as "Full Source Evidence" alongside any "Verified Table Data".
- **Update prompt templates** (`entry.jinja`, `query_process.jinja`, `final_answer.jinja`) to explicitly label and prioritize four evidence tiers: Insight Summary, Full Source Evidence, Verified Table Data, Transformation Output.
- **Add evidence hierarchy conflict resolution**: Verified Table Data > Full Source Evidence > Transformation Output > Insight Summary.
- **Add transformer output guardrails**: transformation outputs (e.g. agribank-change-comparison) are flagged as summaries, not canonical evidence; notebook QA may use them for guidance but must verify against full_text/source_table for detailed claims.
- **Optionally surface evidence metadata** in the API response and frontend: which evidence layer was used, whether a fallback from insight-only to full content occurred.
- **Maintain backward compatibility**: no breaking changes; existing notebooks, sources, and normal prose QA continue to work at current speed.
- **Add comprehensive test coverage** for all evidence routing paths.

## Capabilities

### New Capabilities
- `evidence-routing`: Question classification and automatic evidence-tier selection for notebook QA. Determines whether a question can be answered from insights alone or requires full-content/table fallback.
- `evidence-metadata`: Optional API/UI surface showing which evidence layers were used in each answer and whether fallback occurred.

### Modified Capabilities
- `ask-table-strategy`: Expand table source eligibility from CSV/XLSX-only to any source type with `source_table` records; integrate evidence routing output into the existing `identify_table_source` node.
- `table-qa-prompts`: Update all three ask prompt templates to label four evidence tiers and enforce the evidence hierarchy; add transformer-output guardrails.

## Impact

- **Backend (`open_notebook/graphs/ask.py`)**: New `classify_evidence_need` node; modified `identify_table_source` and `provide_answer` nodes; new `fetch_full_content` helper.
- **Backend (`open_notebook/domain/notebook.py`)**: New method to fetch `full_text` for a set of source IDs scoped to a notebook.
- **Prompts (`prompts/ask/*.jinja`)**: All three templates updated with evidence-tier labels, conflict-resolution rules, and transformer guardrails.
- **API (`api/chat_service.py`)**: Optional `evidence_metadata` field in the ask response payload.
- **Frontend**: Optional evidence badge/warning in the chat UI (non-blocking; can ship in a follow-up wave).
- **Config (`open_notebook/config.py`)**: New environment variables for evidence routing thresholds.
- **Tests**: New test module `tests/test_evidence_routing.py`; expanded cases in `tests/test_ask_*.py`.
- **No database migrations** required — all data structures (source, source_table, source_embedding, source_insight) are unchanged.
