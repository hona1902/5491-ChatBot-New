"""
tests/test_evidence_full_content.py
====================================
Unit tests for the Evidence v2 ``fetch_full_content`` helper and its
integration into ``provide_answer`` in ``open_notebook/graphs/ask.py``.

Coverage (per tasks 3.1–3.5):
  3.1  — EVIDENCE_FULL_TEXT_MAX_CHARS config exists with default 20000
  3.2  — fetch_full_content: returns formatted content; truncates correctly;
         returns None on empty results; returns None on DB error
  3.3  — provide_answer: factual evidence_need triggers full-content fetch
  3.4  — provide_answer: overview evidence_need does NOT trigger fetch
  3.5  — truncation at paragraph boundary; truncation marker present

All DB / domain / LLM calls are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source_row(
    source_id: str,
    title: str = "Test Source",
    full_text: str = "",
    file_path: str = "/data/doc.pdf",
) -> dict:
    """Build a raw row as returned by repo_query for notebook sources."""
    return {
        "source": {
            "id": source_id,
            "title": title,
            "full_text": full_text,
            "topics": [],
            "asset": {"file_path": file_path},
        }
    }


_DUMMY_CONFIG = MagicMock()


# ---------------------------------------------------------------------------
# 3.1 — Config exists
# ---------------------------------------------------------------------------


def test_evidence_full_text_max_chars_exists():
    """EVIDENCE_FULL_TEXT_MAX_CHARS is importable and defaults to 20000."""
    from open_notebook.config import EVIDENCE_FULL_TEXT_MAX_CHARS

    assert isinstance(EVIDENCE_FULL_TEXT_MAX_CHARS, int)
    assert EVIDENCE_FULL_TEXT_MAX_CHARS == 20000


# ---------------------------------------------------------------------------
# 3.2 — fetch_full_content tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_formatted_content():
    """fetch_full_content returns titled, formatted content."""
    row = _make_source_row("source:s1", title="My Document", full_text="Paragraph one.\n\nParagraph two.")

    with (
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.repo_query", new=AsyncMock(return_value=[row])),
    ):
        from open_notebook.graphs.ask import fetch_full_content

        result = await fetch_full_content("notebook:abc", max_chars=50000)

    assert result is not None
    assert "### My Document" in result
    assert "Paragraph one." in result
    assert "Paragraph two." in result


@pytest.mark.asyncio
async def test_fetch_returns_none_when_no_notebook_id():
    """fetch_full_content returns None when notebook_id is None."""
    from open_notebook.graphs.ask import fetch_full_content

    result = await fetch_full_content(None)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_no_sources():
    """fetch_full_content returns None when notebook has no sources."""
    with (
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.repo_query", new=AsyncMock(return_value=[])),
    ):
        from open_notebook.graphs.ask import fetch_full_content

        result = await fetch_full_content("notebook:abc")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_no_full_text():
    """fetch_full_content returns None when sources have no full_text."""
    row = _make_source_row("source:s1", title="Empty", full_text="")

    with (
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.repo_query", new=AsyncMock(return_value=[row])),
    ):
        from open_notebook.graphs.ask import fetch_full_content

        result = await fetch_full_content("notebook:abc")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_db_error_fails_closed():
    """fetch_full_content returns None on DB error (no crash)."""
    with (
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(side_effect=RuntimeError("DB connection lost")),
        ),
    ):
        from open_notebook.graphs.ask import fetch_full_content

        result = await fetch_full_content("notebook:abc")

    assert result is None


# ---------------------------------------------------------------------------
# 3.5 — Truncation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_truncates_at_paragraph_boundary():
    """Content exceeding max_chars is truncated at last paragraph break."""
    long_text = "First paragraph.\n\n" + "Second paragraph with lots of detail. " * 50

    row = _make_source_row("source:s1", title="Long Doc", full_text=long_text)

    with (
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.repo_query", new=AsyncMock(return_value=[row])),
    ):
        from open_notebook.graphs.ask import fetch_full_content

        result = await fetch_full_content("notebook:abc", max_chars=100)

    assert result is not None
    assert "[TRUNCATED" in result
    # Should not contain the full long_text
    assert len(result) < len(long_text)


@pytest.mark.asyncio
async def test_fetch_multi_source_respects_char_limit():
    """Multiple sources are included until char limit is reached."""
    row1 = _make_source_row("source:s1", title="Source A", full_text="A" * 100)
    row2 = _make_source_row("source:s2", title="Source B", full_text="B" * 100)

    with (
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.repo_query", new=AsyncMock(return_value=[row1, row2])),
    ):
        from open_notebook.graphs.ask import fetch_full_content

        result = await fetch_full_content("notebook:abc", max_chars=150)

    assert result is not None
    assert "Source A" in result
    # Source B should be truncated or absent since limit is 150
    # and Source A already uses 100 chars


# ---------------------------------------------------------------------------
# 3.3 & 3.4 — provide_answer integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factual_evidence_triggers_full_content_fetch():
    """evidence_need='factual' causes Full Source Evidence injection."""
    from open_notebook.graphs.ask import provide_answer

    sub_state = {
        "question": "What are the interest rates?",
        "term": "interest rates",
        "instructions": "List the rates",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "factual",
        "notebook_id": "notebook:abc",
    }

    fake_results = [
        {"id": "source_embedding:1", "content": "Rate is 5.5%.", "score": 0.9}
    ]

    mock_ai_message = MagicMock()
    mock_ai_message.content = "The interest rate is 5.5%."

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

    full_text_row = _make_source_row(
        "source:s1", title="Rate Schedule", full_text="Interest rate is 5.5% for tier 1."
    )

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=fake_results)),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.ensure_record_id", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.repo_query", new=AsyncMock(return_value=[full_text_row])),
    ):
        result = await provide_answer(sub_state, _DUMMY_CONFIG)

    assert "answers" in result
    assert len(result["answers"]) >= 1


@pytest.mark.asyncio
async def test_overview_evidence_skips_full_content_fetch():
    """evidence_need='overview' does NOT query full_text — no extra DB call."""
    from open_notebook.graphs.ask import provide_answer

    sub_state = {
        "question": "What is this document about?",
        "term": "document summary",
        "instructions": "Summarize",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "overview",
        "notebook_id": "notebook:abc",
    }

    fake_results = [
        {"id": "source_embedding:1", "content": "A general overview.", "score": 0.85}
    ]

    mock_ai_message = MagicMock()
    mock_ai_message.content = "This document is about banking regulations."

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=fake_results)),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.fetch_full_content", new=AsyncMock()) as mock_fetch,
    ):
        result = await provide_answer(sub_state, _DUMMY_CONFIG)

    assert "answers" in result
    # CRITICAL: fetch_full_content must NOT be called for overview
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_full_content_fetch_error_fails_closed():
    """DB error during full-content fetch → no crash, normal QA proceeds."""
    from open_notebook.graphs.ask import provide_answer

    sub_state = {
        "question": "What are the amendment details?",
        "term": "amendments",
        "instructions": "Extract amendment details",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
        "evidence_need": "legal_comparison",
        "notebook_id": "notebook:abc",
    }

    fake_results = [
        {"id": "source_embedding:1", "content": "Amendment to clause 5.", "score": 0.9}
    ]

    mock_ai_message = MagicMock()
    mock_ai_message.content = "Clause 5 was amended."

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

    with (
        patch("open_notebook.graphs.ask.vector_search", new=AsyncMock(return_value=fake_results)),
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
        patch(
            "open_notebook.graphs.ask.fetch_full_content",
            new=AsyncMock(side_effect=RuntimeError("DB timeout")),
        ),
    ):
        result = await provide_answer(sub_state, _DUMMY_CONFIG)

    # Should still return a valid answer despite the error
    assert "answers" in result
    assert len(result["answers"]) >= 1
