## 1. Strategy Model & Evidence Classification

- [x] 1.1 Add `evidence_need` field to `Strategy` model in `ask.py` with values `overview | factual | legal_comparison`, defaulting to `overview` for backward compatibility
- [x] 1.2 Update `entry.jinja` prompt to instruct the strategy LLM to include `evidence_need` classification in the JSON output
- [x] 1.3 Implement heuristic evidence-need fallback: keyword scanner for table/legal/comparison signals (EN + VI) that upgrades `overview` to `factual` when signal words are detected
- [x] 1.4 Add `evidence_need` to `ThreadState` so it flows through the graph to downstream nodes

- [x] 1.5 Write unit tests: strategy with `evidence_need` field parses correctly; missing field defaults to `overview`; heuristic upgrade works for EN and VI keywords; heuristic never downgrades

## 2. Expand Table Source Eligibility

- [x] 2.1 Remove `_source_is_tabular` CSV/XLSX filter from `identify_table_source` in `ask.py`; replace with direct `source_table COUNT > 0` check for any source type
- [x] 2.2 Update `table_exact_lookup` in `table_lookup.py` to remove the CSV/XLSX file extension guard — allow lookup on any source with `source_table` records
- [x] 2.3 Write unit tests: DOCX source with `source_table` records is selected by `identify_table_source`; PDF source with tables is eligible; source without tables is skipped

## 3. Full-Content Evidence Fetch

- [x] 3.1 Add `EVIDENCE_FULL_TEXT_MAX_CHARS` env var to `config.py` (default 20,000)
- [x] 3.2 Implement `fetch_full_content` async helper: given `notebook_id`, query relevant source(s) for `full_text`, truncate to char limit, return as formatted string or `None`
- [x] 3.3 Integrate `fetch_full_content` into `provide_answer` node: when `evidence_need ∈ {factual, legal_comparison}`, call helper and inject result as `## Full Source Evidence` section
- [x] 3.4 Ensure `provide_answer` skips full-content fetch when `evidence_need = overview` — verify no additional DB query occurs
- [x] 3.5 Write unit tests: factual question triggers full-content fetch; overview question skips it; DB error fails closed; truncation works correctly

## 4. Prompt Updates — Evidence Hierarchy & Guardrails

- [x] 4.1 Update `query_process.jinja`: add four-tier evidence hierarchy section, transformer guardrail, and evidence priority instructions
- [x] 4.2 Update `final_answer.jinja`: add four-tier hierarchy, transformer guardrail, and conflict-resolution instructions; update existing Verified Table Data guardrail
- [x] 4.3 Update `entry.jinja`: add `evidence_need` field definition and classification guidance to the strategy JSON schema
- [x] 4.4 Write prompt snapshot tests: verify all three templates render correctly with and without evidence sections

## 5. Evidence Metadata in API Response

- [x] 5.1 Add `EvidenceMetadata` model (Pydantic) with fields: `evidence_need`, `evidence_layers_used`, `fallback_occurred`
- [x] 5.2 Thread `EvidenceMetadata` through `ThreadState` → `write_final_answer` → API response
- [x] 5.3 Update `chat_service.py` ask endpoint to include optional `evidence_metadata` in the response payload
- [x] 5.4 Write unit tests: metadata is populated correctly for overview, factual, and legal_comparison paths; metadata is `None` when `notebook_id` is absent

## 6. Frontend Evidence Badge (Optional)

- [x] 6.1 Update `use-ask` hook to parse `evidence_metadata` from response
- [x] 6.2 Add optional evidence badge/tooltip component to chat message display
- [x] 6.3 Only render badge when `fallback_occurred = true` or `evidence_need ≠ overview`

## 7. Integration Tests & Regression Suite

- [x] 7.1 Test: insight-only source with table question → falls back to `source_table` / full content
- [x] 7.2 Test: insight-only source with summary question → uses insight normally, no fallback
- [x] 7.3 Test: transformer summary missing a table value → does not override `source_table`
- [x] 7.4 Test: amendment/change-comparison question → cites original clauses/pages
- [x] 7.5 Test: no evidence found for any tier → answer says insufficient evidence
- [x] 7.6 Test: normal prose QA remains unchanged (no regression)
- [x] 7.7 Test: backward compatibility — saved checkpoint without `evidence_need` loads and runs correctly
- [x] 7.8 Test: `notebook_id` absent → legacy path works without evidence routing
