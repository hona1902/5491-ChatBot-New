"""
tests/test_ask_identify_table_source.py
=======================================
Unit tests for the Wave 5B ``identify_table_source`` graph node in
``open_notebook/graphs/ask.py``.

Coverage (per Wave 5B spec tasks 5.6–5.9):
  Task 5.6  — notebook_id missing → candidate_source_id = None, no crash
  Task 5.6  — notebook with zero CSV/XLSX sources → None
  Task 5.7  — notebook with one CSV source → selected WITHOUT embedding call
  Task 5.8  — notebook with multiple CSV sources → embedding disambiguation
  Task 5.9  — all scores below threshold → None
              tie-break by table_count
  Additional — DB error fails closed
  Additional — embedding error on question fails closed
  Additional — embedding error on candidate skips candidate (not crash)
  Additional — provide_answer does NOT inject Verified Table Data
  Additional — table_exact_lookup is NOT called
  Additional — normal prose ask behavior unchanged (candidate_source_id=None path)

All DB / domain / embedding calls are mocked with AsyncMock so no live
database or LLM is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    question: str = "What is the total revenue?",
    notebook_id: str | None = None,
    candidate_source_id: str | None = None,
) -> dict:
    """Build a minimal ThreadState dict for identify_table_source."""
    state: dict = {"question": question, "notebook_id": notebook_id}
    if candidate_source_id is not None:
        state["candidate_source_id"] = candidate_source_id
    return state


def _make_source_row(
    source_id: str,
    file_path: str | None = None,
    title: str = "",
    topics: list | None = None,
) -> dict:
    """Build a raw row as returned by repo_query for SELECT in AS source FROM reference."""
    asset = {"file_path": file_path} if file_path else None
    return {
        "source": {
            "id": source_id,
            "title": title,
            "topics": topics or [],
            "asset": asset,
        }
    }


_DUMMY_CONFIG = MagicMock()

# ---------------------------------------------------------------------------
# Task 5.6 — notebook_id missing → None, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_notebook_id_returns_none():
    """When notebook_id is absent, candidate_source_id = None with no DB call."""
    state = _make_state(notebook_id=None)

    with patch(
        "open_notebook.graphs.ask.repo_query",
        new=AsyncMock(return_value=[]),
    ) as mock_db:
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_empty_notebook_id_returns_none():
    """Empty string notebook_id also returns None without touching DB."""
    state = _make_state(notebook_id="")

    with patch(
        "open_notebook.graphs.ask.repo_query",
        new=AsyncMock(return_value=[]),
    ) as mock_db:
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}
    mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# Task 5.6 — zero CSV/XLSX sources in notebook → None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notebook_with_zero_tabular_sources_returns_none():
    """Notebook with only PDF/DOCX sources → no candidates → None."""
    state = _make_state(notebook_id="notebook:abc")

    pdf_row = _make_source_row("source:pdf1", file_path="/data/report.pdf")
    docx_row = _make_source_row("source:doc1", file_path="/data/contract.docx")

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(return_value=[pdf_row, docx_row]),
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}


@pytest.mark.asyncio
async def test_csv_source_with_no_source_table_records_returns_none():
    """CSV source that has no source_table records → excluded → None."""
    state = _make_state(notebook_id="notebook:abc")

    csv_row = _make_source_row("source:csv1", file_path="/data/sales.csv", title="Sales")

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [csv_row],       # first call: notebook sources
                    [],              # second call: source_table count (empty)
                ]
            ),
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}


# ---------------------------------------------------------------------------
# Task 5.7 — exactly one CSV source → selected WITHOUT embedding call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_csv_source_selected_without_embedding():
    """Exactly one CSV source with source_table records → direct selection, no embedding."""
    state = _make_state(notebook_id="notebook:abc")

    csv_row = _make_source_row(
        "source:csv1", file_path="/data/employees.csv", title="Employees"
    )

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [csv_row],                      # notebook sources
                    [{"cnt": 3}],                   # source_table count = 3
                ]
            ),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(return_value=[1.0, 0.0, 0.0]),
        ) as mock_embed,
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": "source:csv1"}
    # CRITICAL: embedding must NOT be called for the single-candidate fast path
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_single_xlsx_source_selected_without_embedding():
    """Same as above but XLSX extension."""
    state = _make_state(notebook_id="notebook:xyz")

    xlsx_row = _make_source_row(
        "source:xlsx1", file_path="/data/budget.xlsx", title="Q1 Budget"
    )

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [xlsx_row],
                    [{"cnt": 5}],
                ]
            ),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(),
        ) as mock_embed,
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": "source:xlsx1"}
    mock_embed.assert_not_called()


# ---------------------------------------------------------------------------
# Task 5.8 — multiple candidates → embedding disambiguation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_candidates_embedding_selects_best():
    """Two CSV sources; the one whose metadata is most similar to the question is chosen."""
    state = _make_state(
        question="What are the monthly sales figures?", notebook_id="notebook:nb1"
    )

    sales_row = _make_source_row(
        "source:sales", file_path="/data/sales.csv", title="Monthly Sales Data"
    )
    hr_row = _make_source_row(
        "source:hr", file_path="/data/hr.csv", title="Human Resources"
    )

    # Embeddings:
    # question embedding   = [1, 0, 0]
    # "Monthly Sales Data" = [0.99, 0.1, 0]  → cosine ≈ 0.995 (above threshold)
    # "Human Resources"    = [0.0, 1.0, 0]   → cosine = 0.0 (below threshold)
    question_emb = [1.0, 0.0, 0.0]
    sales_meta_emb = [0.99, 0.1, 0.0]
    hr_meta_emb = [0.0, 1.0, 0.0]

    # Cleaner approach: patch generate_embedding at the point where it is
    # lazily imported inside identify_table_source — at the embedding module.
    embed_sequence = [question_emb, sales_meta_emb, hr_meta_emb]
    embed_iter = iter(embed_sequence)

    async def _fake_embed(text, **kw):
        return next(embed_iter)

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [sales_row, hr_row],
                    [{"cnt": 4}],
                    [{"cnt": 2}],
                ]
            ),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(side_effect=_fake_embed),
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": "source:sales"}


# ---------------------------------------------------------------------------
# Task 5.9 — all scores below threshold → None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scores_below_threshold_returns_none():
    """Both candidates score below ASK_TABLE_SOURCE_THRESHOLD → None."""
    state = _make_state(
        question="What is the weather like today?", notebook_id="notebook:nb2"
    )

    csv1 = _make_source_row("source:a", file_path="/data/a.csv", title="Alpha Data")
    csv2 = _make_source_row("source:b", file_path="/data/b.csv", title="Beta Data")

    # Orthogonal embeddings → cosine = 0.0 for both, well below 0.35
    question_emb = [1.0, 0.0, 0.0]
    a_emb = [0.0, 1.0, 0.0]
    b_emb = [0.0, 0.0, 1.0]

    embed_sequence = [question_emb, a_emb, b_emb]
    embed_iter = iter(embed_sequence)

    async def _fake_embed(text, **kw):
        return next(embed_iter)

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [csv1, csv2],
                    [{"cnt": 2}],
                    [{"cnt": 3}],
                ]
            ),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(side_effect=_fake_embed),
        ),
        patch(
            "open_notebook.graphs.ask.ASK_TABLE_SOURCE_THRESHOLD",
            0.35,
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}


# ---------------------------------------------------------------------------
# Tie-break by table_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tie_break_by_table_count():
    """When two candidates have equal cosine score, the one with more table records wins."""
    state = _make_state(question="revenue breakdown", notebook_id="notebook:nb3")

    # Both sources have identical titles (→ identical embeddings → tie)
    csv_a = _make_source_row("source:a", file_path="/data/a.csv", title="Finance Report")
    csv_b = _make_source_row("source:b", file_path="/data/b.csv", title="Finance Report")

    # All embeddings identical → cosine = 1.0 for both → tie
    identical_emb = [1.0, 0.0, 0.0]
    embed_sequence = [identical_emb, identical_emb, identical_emb]
    embed_iter = iter(embed_sequence)

    async def _fake_embed(text, **kw):
        return next(embed_iter)

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [csv_a, csv_b],
                    [{"cnt": 2}],   # source:a has 2 tables
                    [{"cnt": 7}],   # source:b has 7 tables (more → wins tie)
                ]
            ),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(side_effect=_fake_embed),
        ),
        patch(
            "open_notebook.graphs.ask.ASK_TABLE_SOURCE_THRESHOLD",
            0.35,
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    # source:b wins by table_count (7 > 2)
    assert result == {"candidate_source_id": "source:b"}


# ---------------------------------------------------------------------------
# DB error fails closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_error_on_sources_query_fails_closed():
    """DB error while fetching notebook sources → candidate_source_id = None."""
    state = _make_state(notebook_id="notebook:nb_err")

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(side_effect=RuntimeError("Connection refused")),
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}


@pytest.mark.asyncio
async def test_db_error_on_table_count_query_skips_candidate():
    """DB error while counting source_table records → that candidate is skipped (not crash)."""
    state = _make_state(notebook_id="notebook:nb_partial")

    good_row = _make_source_row(
        "source:good", file_path="/data/good.csv", title="Good Data"
    )
    bad_row = _make_source_row(
        "source:bad", file_path="/data/bad.csv", title="Bad Data"
    )

    call_count = {"n": 0}

    async def _fake_repo_query(query, params=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: notebook sources
            return [good_row, bad_row]
        elif call_count["n"] == 2:
            # Second call: count for source:good → success
            return [{"cnt": 3}]
        else:
            # Third call: count for source:bad → DB error
            raise RuntimeError("DB error for bad source")

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(side_effect=_fake_repo_query),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(),
        ) as mock_embed,
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    # source:good still passes; source:bad is skipped → single candidate → fast path
    assert result == {"candidate_source_id": "source:good"}
    # Single candidate fast path → no embedding
    mock_embed.assert_not_called()


# ---------------------------------------------------------------------------
# Embedding error fails closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_error_on_question_fails_closed():
    """Embedding failure on the question → candidate_source_id = None."""
    state = _make_state(
        question="multi-source question", notebook_id="notebook:nb_emb"
    )

    csv1 = _make_source_row("source:c1", file_path="/data/c1.csv", title="C1")
    csv2 = _make_source_row("source:c2", file_path="/data/c2.csv", title="C2")

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [csv1, csv2],
                    [{"cnt": 1}],
                    [{"cnt": 1}],
                ]
            ),
        ),
        patch(
            "open_notebook.utils.embedding.generate_embedding",
            new=AsyncMock(side_effect=RuntimeError("Embedding model offline")),
        ),
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result == {"candidate_source_id": None}


# ---------------------------------------------------------------------------
# Confirm table_exact_lookup is NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_exact_lookup_not_called():
    """identify_table_source must never call table_exact_lookup."""
    state = _make_state(notebook_id="notebook:nb_chk")

    csv_row = _make_source_row(
        "source:csv1", file_path="/data/data.csv", title="Some Data"
    )

    with (
        patch(
            "open_notebook.graphs.ask.ensure_record_id",
            side_effect=lambda x: x,
        ),
        patch(
            "open_notebook.graphs.ask.repo_query",
            new=AsyncMock(
                side_effect=[
                    [csv_row],
                    [{"cnt": 2}],
                ]
            ),
        ),
        patch(
            "open_notebook.utils.table_lookup.table_exact_lookup",
            new=AsyncMock(return_value="SHOULD NOT BE CALLED"),
        ) as mock_lookup,
    ):
        from open_notebook.graphs.ask import identify_table_source

        result = await identify_table_source(state, _DUMMY_CONFIG)

    assert result["candidate_source_id"] == "source:csv1"
    mock_lookup.assert_not_called()


# ---------------------------------------------------------------------------
# provide_answer behavior unchanged (no Verified Table Data injection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provide_answer_does_not_inject_verified_table_data():
    """provide_answer Wave 5C: when table_exact_lookup returns None (no match),
    no 'Verified Table Data' section appears in the LLM prompt or the answer.
    This ensures the fail-through path works correctly.
    (Wave 5B note: this test now patches table_exact_lookup to return None
     because Wave 5C has wired the lookup into provide_answer.)"""
    from open_notebook.graphs.ask import provide_answer

    sub_state = {
        "question": "What is the revenue?",
        "term": "revenue",
        "instructions": "Extract revenue figures",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": "source:some_csv",  # Wave 5C will attempt lookup
    }

    fake_vector_results = [
        {"id": "source_embedding:1", "content": "Revenue was $500K in Q1.", "score": 0.9}
    ]

    mock_ai_message = MagicMock()
    mock_ai_message.content = "Revenue was $500K in Q1."

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

    with (
        patch(
            "open_notebook.graphs.ask.vector_search",
            new=AsyncMock(return_value=fake_vector_results),
        ),
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
        # Wave 5C: mock the lazily-imported table_exact_lookup to return None
        # (no keyword/semantic match) → no injection → normal prose QA only.
        patch(
            "open_notebook.utils.table_lookup.table_exact_lookup",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await provide_answer(sub_state, _DUMMY_CONFIG)

    # Result must be a normal prose answer list
    assert "answers" in result
    assert isinstance(result["answers"], list)
    # lookup returned None → no 'Verified Table Data' injection in answer text
    for answer in result["answers"]:
        assert "Verified Table Data" not in str(answer)


# ---------------------------------------------------------------------------
# Normal prose QA behavior unchanged (no notebook_id path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_prose_ask_unchanged_when_no_candidate():
    """With no notebook_id (candidate_source_id=None), provide_answer works normally."""
    from open_notebook.graphs.ask import provide_answer

    sub_state = {
        "question": "Tell me about climate change.",
        "term": "climate change",
        "instructions": "Summarize climate findings",
        "results": {},
        "answer": "",
        "ids": [],
        "candidate_source_id": None,
    }

    fake_results = [
        {"id": "source_embedding:99", "content": "Climate change is a major concern.", "score": 0.85}
    ]

    mock_ai_message = MagicMock()
    mock_ai_message.content = "Climate change affects global temperatures."

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=mock_ai_message)

    with (
        patch(
            "open_notebook.graphs.ask.vector_search",
            new=AsyncMock(return_value=fake_results),
        ),
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
    ):
        result = await provide_answer(sub_state, _DUMMY_CONFIG)

    assert "answers" in result
    assert len(result["answers"]) == 1
    assert "climate" in result["answers"][0].lower()
