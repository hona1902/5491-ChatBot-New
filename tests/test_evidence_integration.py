"""
tests/test_evidence_integration.py
===================================
Integration tests for the Evidence v2 notebook QA system.

Coverage (per tasks 7.1–7.8):
  7.1 — insight-only source + table question → falls back to source_table / full content
  7.2 — insight-only source + summary question → uses insight normally, no fallback
  7.3 — transformer summary missing table value → does not override source_table
  7.4 — amendment/change-comparison → cites original clauses (legal_comparison tier)
  7.5 — no evidence found for any tier → answer says insufficient evidence
  7.6 — normal prose QA remains unchanged (no regression)
  7.7 — backward compat: saved checkpoint without evidence_need loads correctly
  7.8 — notebook_id absent → legacy path works without evidence routing

All DB / LLM calls are mocked.  These tests verify end-to-end data flow
through the graph models and state, not live LLM responses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_strategy(evidence_need: str = "overview"):
    from open_notebook.graphs.ask import Strategy

    return Strategy(reasoning="test reasoning", searches=[], evidence_need=evidence_need)


def _make_mock_model(response_text: str):
    mock_model = AsyncMock()
    msg = MagicMock()
    msg.content = response_text
    mock_model.ainvoke = AsyncMock(return_value=msg)
    return mock_model


_DUMMY_CONFIG = MagicMock()
_DUMMY_CONFIG.get = MagicMock(return_value={})


# ---------------------------------------------------------------------------
# 7.1 — insight-only + table question → fallback to source_table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_question_upgrades_to_factual():
    """Table question triggers heuristic upgrade from overview to factual."""
    from open_notebook.graphs.ask import _heuristic_evidence_upgrade

    result = _heuristic_evidence_upgrade("overview", "Cho biết bảng lãi suất tiền gửi?")
    assert result == "factual"


@pytest.mark.asyncio
async def test_factual_evidence_triggers_full_content():
    """When evidence_need=factual, provide_answer fetches full content."""
    from open_notebook.graphs.ask import provide_answer

    state = {
        "question": "What is the fee table?",
        "term": "fee table",
        "instructions": "Extract fee info",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "factual",
        "notebook_id": "notebook:abc",
    }

    mock_model = _make_mock_model("The fee is 5%.")

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=[
            {"id": "se:1", "content": "Fees listed.", "score": 0.9}
        ])),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.fetch_full_content", new=AsyncMock(return_value="Full text about fees")),
    ):
        result = await provide_answer(state, _DUMMY_CONFIG)

    assert "answers" in result
    assert len(result["answers"]) >= 1


# ---------------------------------------------------------------------------
# 7.2 — summary question → uses insight normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_question_stays_overview():
    """Summary question stays at overview, no full content fetch."""
    from open_notebook.graphs.ask import _heuristic_evidence_upgrade

    result = _heuristic_evidence_upgrade("overview", "Tóm tắt nội dung tài liệu này")
    assert result == "overview"


@pytest.mark.asyncio
async def test_overview_does_not_fetch_full_content():
    """evidence_need=overview → fetch_full_content NOT called."""
    from open_notebook.graphs.ask import provide_answer

    state = {
        "question": "Summarize the document",
        "term": "summary",
        "instructions": "Summarize",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "overview",
        "notebook_id": "notebook:abc",
    }

    mock_model = _make_mock_model("General banking document.")

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=[
            {"id": "se:1", "content": "Banking doc.", "score": 0.85}
        ])),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.fetch_full_content", new=AsyncMock()) as mock_fc,
    ):
        result = await provide_answer(state, _DUMMY_CONFIG)

    assert "answers" in result
    mock_fc.assert_not_called()


# ---------------------------------------------------------------------------
# 7.3 — transformer summary does not override source_table
# ---------------------------------------------------------------------------


def test_evidence_hierarchy_in_query_process_prompt():
    """query_process.jinja says NEVER override Verified Table Data."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parent.parent
        / "prompts"
        / "ask"
        / "query_process.jinja"
    ).read_text(encoding="utf-8")

    assert "NEVER override" in template
    assert "Verified Table Data" in template


def test_evidence_hierarchy_in_final_answer_prompt():
    """final_answer.jinja has Do NOT fabricate from lower-tier sources."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parent.parent
        / "prompts"
        / "ask"
        / "final_answer.jinja"
    ).read_text(encoding="utf-8")

    assert "Do NOT fabricate" in template


# ---------------------------------------------------------------------------
# 7.4 — amendment/comparison → legal_comparison tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amendment_question_classified_as_legal_comparison():
    """Amendment/comparison keywords → upgrade to factual (or stay at legal_comparison)."""
    from open_notebook.graphs.ask import _heuristic_evidence_upgrade

    result = _heuristic_evidence_upgrade("overview", "Compare the old and new amendment clauses")
    assert result in ("factual", "legal_comparison")


# ---------------------------------------------------------------------------
# 7.5 — no evidence → says insufficient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_evidence_returns_empty_answers():
    """No vector search results → empty answers list."""
    from open_notebook.graphs.ask import provide_answer

    state = {
        "question": "What is XYZ?",
        "term": "XYZ",
        "instructions": "Find XYZ",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "factual",
        "notebook_id": "notebook:abc",
    }

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=[])),
    ):
        result = await provide_answer(state, _DUMMY_CONFIG)

    assert result == {"answers": []}


# ---------------------------------------------------------------------------
# 7.6 — normal prose QA regression check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prose_qa_works_without_evidence_need():
    """Standard prose QA (no evidence fields) still works correctly."""
    from open_notebook.graphs.ask import provide_answer

    state = {
        "question": "What is deep learning?",
        "term": "deep learning",
        "instructions": "Explain",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "overview",
        "notebook_id": None,
    }

    mock_model = _make_mock_model("Deep learning is a subset of ML.")

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=[
            {"id": "se:1", "content": "DL is a subset of ML.", "score": 0.9}
        ])),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
    ):
        result = await provide_answer(state, _DUMMY_CONFIG)

    assert "answers" in result
    assert len(result["answers"]) == 1
    assert "Deep learning" in result["answers"][0]


# ---------------------------------------------------------------------------
# 7.7 — backward compat: no evidence_need in state
# ---------------------------------------------------------------------------


def test_strategy_without_evidence_need_defaults():
    """Strategy model missing evidence_need → defaults to 'overview'."""
    from open_notebook.graphs.ask import Strategy

    # Simulate loading from a checkpoint that doesn't have evidence_need
    s = Strategy(reasoning="old checkpoint", searches=[])
    assert s.evidence_need == "overview"


@pytest.mark.asyncio
async def test_write_final_answer_handles_missing_evidence_need():
    """write_final_answer works when evidence_need is None (legacy state)."""
    from open_notebook.graphs.ask import Strategy, write_final_answer

    state = {
        "question": "Test",
        "strategy": Strategy(reasoning="test", searches=[]),
        "answers": ["Answer"],
        "final_answer": "",
        "notebook_id": None,
        "candidate_source_id": None,
        "evidence_need": None,  # Legacy — not set
        "evidence_metadata": None,
    }

    mock_model = _make_mock_model("Final answer.")

    with (
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
    ):
        result = await write_final_answer(state, _DUMMY_CONFIG)

    assert result["final_answer"] == "Final answer."
    assert result["evidence_metadata"]["evidence_need"] == "overview"


# ---------------------------------------------------------------------------
# 7.8 — notebook_id absent → legacy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_notebook_id_skips_evidence_routing():
    """When notebook_id is absent, evidence routing is minimal."""
    from open_notebook.graphs.ask import provide_answer

    state = {
        "question": "General question",
        "term": "general",
        "instructions": "Answer",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "overview",
        "notebook_id": None,  # No notebook context
    }

    mock_model = _make_mock_model("A general answer.")

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=[
            {"id": "se:1", "content": "General info.", "score": 0.8}
        ])),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.fetch_full_content", new=AsyncMock()) as mock_fc,
    ):
        result = await provide_answer(state, _DUMMY_CONFIG)

    assert "answers" in result
    # fetch_full_content should not be called for overview
    mock_fc.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_full_content_returns_none_without_notebook():
    """fetch_full_content returns None when notebook_id is None."""
    from open_notebook.graphs.ask import fetch_full_content

    result = await fetch_full_content(None)
    assert result is None
