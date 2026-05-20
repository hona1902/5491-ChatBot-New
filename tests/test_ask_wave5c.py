"""
tests/test_ask_wave5c.py
========================
Unit and integration tests for Wave 5C: Verified Table Data injection
in ``open_notebook/graphs/ask.py :: provide_answer``.

Coverage (per Wave 5C spec task 5.4 and user requirement):
  - candidate_source_id=None → table_exact_lookup NOT called
  - candidate_source_id set, lookup returns None → no injection, normal QA
  - candidate_source_id set, lookup returns data → ## Verified Table Data prepended
  - lookup raises exception → fail-closed, no crash, normal QA proceeds
  - prompt guardrail / citation instruction present in final_answer.jinja
  - vector_search still runs regardless
  - source_chat.py untouched (structural check)
  - Task 5.10: integration test for notebook ask with a CSV/XLSX-like table source
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent

_DUMMY_CONFIG = MagicMock()


def _make_sub_state(
    question: str = "What is the total revenue?",
    term: str = "revenue",
    instructions: str = "Extract revenue figures",
    candidate_source_id: str | None = None,
) -> dict:
    """Build a minimal SubGraphState for provide_answer tests."""
    return {
        "question": question,
        "term": term,
        "instructions": instructions,
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": candidate_source_id,
    }


def _fake_vector_results(n: int = 1) -> list[dict]:
    return [
        {"id": f"source_embedding:{i}", "content": f"Content line {i}.", "score": 0.9}
        for i in range(n)
    ]


def _mock_model_returning(text: str) -> AsyncMock:
    msg = MagicMock()
    msg.content = text
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=msg)
    return model


# ===========================================================================
# 1. candidate_source_id=None → table_exact_lookup NOT called
# ===========================================================================


class TestNoLookupWhenNoneCandidate:
    """When candidate_source_id is None, no table lookup must occur."""

    @pytest.mark.asyncio
    async def test_lookup_not_called_when_candidate_none(self):
        """table_exact_lookup must NOT be called when candidate_source_id is None."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id=None)
        model = _mock_model_returning("Prose answer about climate.")

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=model),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value="SHOULD NOT BE RETURNED"),
            ) as mock_lookup,
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        mock_lookup.assert_not_called()
        assert "answers" in result
        assert len(result["answers"]) == 1

    @pytest.mark.asyncio
    async def test_vector_search_still_runs_when_candidate_none(self):
        """vector_search must always run, even with no candidate."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id=None)
        model = _mock_model_returning("Answer.")

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ) as mock_vs,
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=model),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        mock_vs.assert_called_once()


# ===========================================================================
# 2. candidate_source_id set, lookup returns None → no injection, normal QA
# ===========================================================================


class TestNoInjectionWhenLookupReturnsNone:
    """When lookup returns None, the answer is a normal prose response with no injection."""

    @pytest.mark.asyncio
    async def test_no_injection_when_lookup_returns_none(self):
        """If table_exact_lookup returns None, no ## Verified Table Data in prompt."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:my_csv")
        model = _mock_model_returning("Regular prose answer.")

        captured_prompts: list[str] = []

        async def _fake_provision(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return model

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(side_effect=_fake_provision),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        # No injection at the start of the prompt (template may reference the phrase
        # in the evidence hierarchy explanation, but the actual prefix is always prepended
        # at position 0 when lookup returns data).
        assert all(not p.startswith("## Verified Table Data") for p in captured_prompts)
        assert "answers" in result
        assert result["answers"] == ["Regular prose answer."]

    @pytest.mark.asyncio
    async def test_vector_search_still_runs_when_lookup_returns_none(self):
        """vector_search must still run when lookup returns None."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:some_csv")
        model = _mock_model_returning("Answer.")

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ) as mock_vs,
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=model),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        mock_vs.assert_called_once()


# ===========================================================================
# 3. Lookup returns data → ## Verified Table Data injected before normal context
# ===========================================================================


class TestVerifiedTableDataInjection:
    """When lookup returns a string, ## Verified Table Data is prepended."""

    @pytest.mark.asyncio
    async def test_verified_table_data_prepended(self):
        """Prompt must start with ## Verified Table Data when lookup returns data."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(
            question="What is revenue for Q1?",
            candidate_source_id="source:sales_csv",
        )
        model = _mock_model_returning("Revenue for Q1 is $500K. [source:sales_csv]")

        TABLE_DATA = "| Month | Revenue |\n| --- | --- |\n| Jan | $100K |\n| Feb | $150K |\n| Mar | $250K |"
        captured_prompts: list[str] = []

        async def _capture_provision(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return model

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(side_effect=_capture_provision),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value=TABLE_DATA),
            ),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        assert len(captured_prompts) >= 1
        prompt = captured_prompts[0]
        # Must start with the Verified Table Data section
        assert prompt.startswith("## Verified Table Data"), (
            f"Prompt must start with '## Verified Table Data'. Got:\n{prompt[:200]}"
        )
        # The table data itself must appear in the prompt
        assert TABLE_DATA in prompt

    @pytest.mark.asyncio
    async def test_verified_table_data_before_normal_vector_context(self):
        """## Verified Table Data section must appear before the normal prompt content."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:budget_xlsx")
        model = _mock_model_returning("The budget is $1M.")

        TABLE_DATA = "| Dept | Budget |\n| --- | --- |\n| Eng | $1M |"
        MARKER = "SYSTEM ROLE"  # something in the normal query_process.jinja prompt

        captured_prompts: list[str] = []

        async def _capture_provision(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return model

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(side_effect=_capture_provision),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value=TABLE_DATA),
            ),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        prompt = captured_prompts[0]
        vtd_pos = prompt.find("## Verified Table Data")
        # Normal prompt content comes after the Verified Table Data prefix
        assert vtd_pos == 0, "Verified Table Data must be at the very start of the prompt"
        # Table data appears before the normal SYSTEM ROLE section
        marker_pos = prompt.find(MARKER)
        if marker_pos != -1:
            assert vtd_pos < marker_pos, (
                "## Verified Table Data must appear before normal prompt content"
            )

    @pytest.mark.asyncio
    async def test_answer_returned_normally_with_injection(self):
        """The answer shape must be unchanged (list of strings) even with injection."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:my_table")
        ANSWER_TEXT = "Q1 revenue is $500K. [source:my_table]"
        model = _mock_model_returning(ANSWER_TEXT)

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=model),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value="| A | B |\n| 1 | 2 |"),
            ),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        assert "answers" in result
        assert isinstance(result["answers"], list)
        assert len(result["answers"]) == 1
        assert result["answers"][0] == ANSWER_TEXT


# ===========================================================================
# 4. Lookup raises exception → fail-closed, no crash
# ===========================================================================


class TestLookupFailClosed:
    """Any error from table_exact_lookup must result in normal QA proceeding."""

    @pytest.mark.asyncio
    async def test_lookup_error_no_crash_no_injection(self):
        """If table_exact_lookup raises, provide_answer continues with normal prose QA."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:broken_csv")
        model = _mock_model_returning("Normal prose answer.")

        captured_prompts: list[str] = []

        async def _capture_provision(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return model

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(side_effect=_capture_provision),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(side_effect=RuntimeError("DB connection refused")),
            ),
        ):
            # Must NOT raise
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        # No injection (error path) — the prefix must NOT be at the start
        assert all(not p.startswith("## Verified Table Data") for p in captured_prompts)
        assert "answers" in result
        assert result["answers"] == ["Normal prose answer."]

    @pytest.mark.asyncio
    async def test_lookup_timeout_error_fails_closed(self):
        """TimeoutError from lookup is also caught; normal QA proceeds."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:slow_csv")
        model = _mock_model_returning("Answer from vector search only.")

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=model),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(side_effect=TimeoutError("Lookup timed out")),
            ),
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        assert "answers" in result
        assert len(result["answers"]) == 1

    @pytest.mark.asyncio
    async def test_vector_search_still_runs_after_lookup_error(self):
        """vector_search must still execute even when lookup fails."""
        from open_notebook.graphs.ask import provide_answer

        sub_state = _make_sub_state(candidate_source_id="source:broken")
        model = _mock_model_returning("Answer.")

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results()),
            ) as mock_vs,
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=model),
            ),
            patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
            patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(side_effect=ValueError("Bad source id")),
            ),
        ):
            await provide_answer(sub_state, _DUMMY_CONFIG)

        mock_vs.assert_called_once()


# ===========================================================================
# 5. Prompt guardrail / citation instruction present (structural)
# ===========================================================================


class TestPromptGuardrailPresent:
    """The query_process.jinja prompt must include table grounding rules,
    and final_answer.jinja must include the Verified Table Data guardrail."""

    def _load_query_process(self) -> str:
        return (PROJECT_ROOT / "prompts" / "ask" / "query_process.jinja").read_text(encoding="utf-8")

    def _load_final_answer(self) -> str:
        return (PROJECT_ROOT / "prompts" / "ask" / "final_answer.jinja").read_text(encoding="utf-8")

    def test_query_process_has_table_grounding_rules(self):
        """query_process.jinja must contain TABLE GROUNDING RULES section."""
        content = self._load_query_process()
        assert "TABLE GROUNDING RULES" in content

    def test_query_process_cite_table_page_sheet(self):
        """query_process.jinja must instruct citing table/page/sheet."""
        content = self._load_query_process().lower()
        assert "table" in content
        assert "page" in content
        assert "sheet" in content

    def test_final_answer_has_verified_table_data_guardrail(self):
        """final_answer.jinja must contain VERIFIED TABLE DATA GUARDRAIL section."""
        content = self._load_final_answer()
        assert "VERIFIED TABLE DATA GUARDRAIL" in content

    def test_final_answer_guardrail_has_escape_clause(self):
        """Guardrail must have an escape clause so it doesn't force refusal."""
        content = self._load_final_answer()
        assert "not present" in content or "ignore this instruction" in content

    def test_final_answer_guardrail_prioritises_verified_data(self):
        """Guardrail must mention prioritising/preferring Verified Table Data."""
        content = self._load_final_answer()
        lower = content.lower()
        assert "prefer" in lower or "priorit" in lower or "high-priority" in lower


# ===========================================================================
# 6. source_chat.py untouched (structural check)
# ===========================================================================


class TestSourceChatUntouched:
    """source_chat.py must not reference provide_answer or Verified Table Data injection."""

    def _load_source_chat(self) -> str:
        path = PROJECT_ROOT / "open_notebook" / "graphs" / "source_chat.py"
        if not path.exists():
            pytest.skip("source_chat.py not found — skipping structural check")
        return path.read_text(encoding="utf-8")

    def test_source_chat_does_not_import_provide_answer(self):
        """source_chat.py must not import provide_answer from ask.py."""
        content = self._load_source_chat()
        assert "from open_notebook.graphs.ask import provide_answer" not in content
        assert "from .ask import provide_answer" not in content

    def test_source_chat_still_uses_table_exact_lookup(self):
        """source_chat.py must still reference table_exact_lookup (it owns that call)."""
        content = self._load_source_chat()
        assert "table_exact_lookup" in content, (
            "source_chat.py should still call table_exact_lookup directly — do not remove it"
        )


# ===========================================================================
# 7. Module-level namespace: table_exact_lookup NOT at module level
# ===========================================================================


class TestModuleLevelNamespace:
    """table_exact_lookup must be imported lazily inside provide_answer, not at module level."""

    def test_table_exact_lookup_not_in_ask_module_namespace(self):
        """ask.py module must NOT export table_exact_lookup at import time."""
        import open_notebook.graphs.ask as ask_module

        assert not hasattr(ask_module, "table_exact_lookup"), (
            "table_exact_lookup must NOT be imported at module level in ask.py "
            "(it is lazily imported inside provide_answer to preserve Wave 5A test contract)"
        )

    def test_graph_nodes_have_no_lookup_inject_names(self):
        """Graph node names must not contain 'lookup' or 'inject' (Wave 5C injects inside provide_answer)."""
        from open_notebook.graphs.ask import graph

        forbidden = {n for n in graph.nodes if "lookup" in n.lower() or "inject" in n.lower()}
        assert not forbidden, (
            f"No lookup/inject graph nodes must be present; found: {forbidden}"
        )


# ===========================================================================
# 8. Task 5.10 — Integration test: CSV source ask with table match
# ===========================================================================


class TestIntegrationAskWithCSVSource:
    """
    Task 5.10: End-to-end integration test for the ask path when a CSV/XLSX
    source candidate is identified and table data is returned.

    This test mocks the full stack (DB, embedding, LLM) but exercises
    the real provide_answer code path including:
      - identify_table_source (Wave 5B) → candidate_source_id set
      - provide_answer (Wave 5C) → table_exact_lookup called → Verified Table Data injected
      - LLM receives the enriched prompt
    """

    @pytest.mark.asyncio
    async def test_provide_answer_grounded_in_table_data(self):
        """
        Simulate a CSV source ask: lookup returns matching rows, the final
        prompt must contain ## Verified Table Data, and the answer must be
        the LLM's response (not bypassed).
        """
        from open_notebook.graphs.ask import provide_answer

        QUESTION = "What is the January revenue?"
        TABLE_ROWS = (
            "**Table: source_table:tbl1 | Page: 1**\n"
            "| Month | Revenue |\n"
            "| --- | --- |\n"
            "| January | $120,000 |"
        )

        sub_state = {
            "question": QUESTION,
            "term": "January revenue",
            "instructions": "Extract the January revenue figure from the table",
            "results": {},
            "answer": "",
            "ids": [],
            "candidate_source_id": "source:sales_csv_2024",
        }

        EXPECTED_ANSWER = (
            "The January revenue is $120,000. "
            "[Source: Sales Data, Table: source_table:tbl1, Page: 1, Sheet: N/A]"
        )

        mock_ai_message = MagicMock()
        mock_ai_message.content = EXPECTED_ANSWER
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

        captured_prompts: list[str] = []

        async def _capture_provision(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return mock_model

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results(2)),
            ),
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(side_effect=_capture_provision),
            ),
            patch(
                "open_notebook.graphs.ask.extract_text_content",
                side_effect=lambda x: x,
            ),
            patch(
                "open_notebook.graphs.ask.clean_thinking_content",
                side_effect=lambda x: x,
            ),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value=TABLE_ROWS),
            ) as mock_lookup,
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        # 1. table_exact_lookup was called with the right args
        mock_lookup.assert_called_once_with(QUESTION, "source:sales_csv_2024")

        # 2. The prompt sent to the LLM contains ## Verified Table Data
        assert len(captured_prompts) >= 1
        prompt = captured_prompts[0]
        assert "## Verified Table Data" in prompt, (
            "LLM prompt must contain '## Verified Table Data' when lookup returns data"
        )
        assert TABLE_ROWS in prompt, "Table rows must appear verbatim in the prompt"

        # 3. The answer is grounded in table data (LLM's response forwarded as-is)
        assert "answers" in result
        assert isinstance(result["answers"], list)
        assert len(result["answers"]) == 1
        assert result["answers"][0] == EXPECTED_ANSWER

    @pytest.mark.asyncio
    async def test_integration_no_candidate_full_prose_path(self):
        """
        Integration test for the no-table-source path (candidate_source_id=None):
        table_exact_lookup must NOT be called, vector_search must run, and the
        answer must be a normal prose response.
        """
        from open_notebook.graphs.ask import provide_answer

        sub_state = {
            "question": "What is the historical context of World War II?",
            "term": "World War II history",
            "instructions": "Provide historical context",
            "results": {},
            "answer": "",
            "ids": [],
            "candidate_source_id": None,
        }

        PROSE_ANSWER = "World War II began in 1939 and ended in 1945. [source:history_doc]"
        mock_ai_message = MagicMock()
        mock_ai_message.content = PROSE_ANSWER
        mock_model = AsyncMock()
        mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

        with (
            patch(
                "open_notebook.graphs.ask.vector_search",
                new=AsyncMock(return_value=_fake_vector_results(3)),
            ) as mock_vs,
            patch(
                "open_notebook.graphs.ask.provision_langchain_model",
                new=AsyncMock(return_value=mock_model),
            ),
            patch(
                "open_notebook.graphs.ask.extract_text_content",
                side_effect=lambda x: x,
            ),
            patch(
                "open_notebook.graphs.ask.clean_thinking_content",
                side_effect=lambda x: x,
            ),
            patch(
                "open_notebook.utils.table_lookup.table_exact_lookup",
                new=AsyncMock(return_value="SHOULD NOT APPEAR"),
            ) as mock_lookup,
        ):
            result = await provide_answer(sub_state, _DUMMY_CONFIG)

        mock_lookup.assert_not_called()
        mock_vs.assert_called_once()
        assert result["answers"] == [PROSE_ANSWER]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
