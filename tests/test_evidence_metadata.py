"""
tests/test_evidence_metadata.py
================================
Unit tests for Evidence v2 metadata model and its threading through
write_final_answer → API response.

Coverage (per tasks 5.1–5.4):
  5.1  — EvidenceMetadata Pydantic model exists with correct fields
  5.2  — write_final_answer populates evidence_metadata in return dict
  5.3  — AskResponse API model includes optional evidence_metadata
  5.4  — Metadata correct for overview, factual, legal_comparison paths;
         None when notebook_id is absent
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 5.1 — EvidenceMetadata model
# ---------------------------------------------------------------------------


class TestEvidenceMetadataModel:
    """EvidenceMetadata Pydantic model validation."""

    def test_model_exists_and_importable(self):
        from open_notebook.graphs.ask import EvidenceMetadata

        assert EvidenceMetadata is not None

    def test_default_values(self):
        from open_notebook.graphs.ask import EvidenceMetadata

        meta = EvidenceMetadata()
        assert meta.evidence_need == "overview"
        assert meta.evidence_layers_used == []
        assert meta.fallback_occurred is False

    def test_factual_values(self):
        from open_notebook.graphs.ask import EvidenceMetadata

        meta = EvidenceMetadata(
            evidence_need="factual",
            evidence_layers_used=["verified_table_data", "vector_search"],
            fallback_occurred=True,
        )
        assert meta.evidence_need == "factual"
        assert "verified_table_data" in meta.evidence_layers_used
        assert meta.fallback_occurred is True

    def test_model_dump(self):
        from open_notebook.graphs.ask import EvidenceMetadata

        meta = EvidenceMetadata(
            evidence_need="legal_comparison",
            evidence_layers_used=["full_source_evidence", "vector_search"],
        )
        d = meta.model_dump()
        assert d["evidence_need"] == "legal_comparison"
        assert isinstance(d["evidence_layers_used"], list)
        assert d["fallback_occurred"] is False


# ---------------------------------------------------------------------------
# 5.3 — AskResponse includes evidence_metadata
# ---------------------------------------------------------------------------


class TestAskResponseModel:
    def test_ask_response_has_evidence_metadata_field(self):
        from api.models import AskResponse

        assert "evidence_metadata" in AskResponse.model_fields

    def test_ask_response_evidence_metadata_optional(self):
        from api.models import AskResponse

        resp = AskResponse(answer="Test answer", question="Test question")
        assert resp.evidence_metadata is None

    def test_ask_response_with_evidence_metadata(self):
        from api.models import AskResponse

        resp = AskResponse(
            answer="Test",
            question="Q",
            evidence_metadata={
                "evidence_need": "factual",
                "evidence_layers_used": ["vector_search"],
                "fallback_occurred": False,
            },
        )
        assert resp.evidence_metadata is not None
        assert resp.evidence_metadata["evidence_need"] == "factual"


# ---------------------------------------------------------------------------
# 5.2 & 5.4 — write_final_answer populates metadata
# ---------------------------------------------------------------------------


def _make_mock_ai_message(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    return msg


@pytest.mark.asyncio
async def test_write_final_answer_returns_evidence_metadata_overview():
    """Overview path → metadata shows overview, vector_search only."""
    from open_notebook.graphs.ask import Strategy, write_final_answer

    state = {
        "question": "What is this about?",
        "strategy": Strategy(reasoning="general summary", searches=[]),
        "answers": ["This is about banking."],
        "final_answer": "",
        "notebook_id": None,
        "candidate_source_id": None,
        "evidence_need": "overview",
        "evidence_metadata": None,
    }

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=_make_mock_ai_message("Banking doc."))

    config = MagicMock()
    config.get = MagicMock(return_value={})

    with (
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
    ):
        result = await write_final_answer(state, config)

    assert "evidence_metadata" in result
    meta = result["evidence_metadata"]
    assert meta["evidence_need"] == "overview"
    assert "vector_search" in meta["evidence_layers_used"]
    assert meta["fallback_occurred"] is False


@pytest.mark.asyncio
async def test_write_final_answer_detects_verified_table_data():
    """When answers contain 'Verified Table Data', metadata includes that layer."""
    from open_notebook.graphs.ask import Strategy, write_final_answer

    state = {
        "question": "What is the rate?",
        "strategy": Strategy(reasoning="table lookup", searches=[], evidence_need="factual"),
        "answers": ["## Verified Table Data\n\n| Rate | 5.5% |"],
        "final_answer": "",
        "notebook_id": "notebook:abc",
        "candidate_source_id": "source:s1",
        "evidence_need": "factual",
        "evidence_metadata": None,
    }

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=_make_mock_ai_message("Rate is 5.5%"))

    config = MagicMock()
    config.get = MagicMock(return_value={})

    with (
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
    ):
        result = await write_final_answer(state, config)

    meta = result["evidence_metadata"]
    assert meta["evidence_need"] == "factual"
    assert "verified_table_data" in meta["evidence_layers_used"]


@pytest.mark.asyncio
async def test_write_final_answer_detects_fallback():
    """When evidence_need was upgraded from strategy's value, fallback_occurred=True."""
    from open_notebook.graphs.ask import Strategy, write_final_answer

    # Strategy says overview, but heuristic upgraded to factual
    state = {
        "question": "What is the interest rate?",
        "strategy": Strategy(reasoning="general", searches=[], evidence_need="overview"),
        "answers": ["The rate is 5.5%."],
        "final_answer": "",
        "notebook_id": "notebook:abc",
        "candidate_source_id": None,
        "evidence_need": "factual",  # Upgraded by heuristic
        "evidence_metadata": None,
    }

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=_make_mock_ai_message("5.5%"))

    config = MagicMock()
    config.get = MagicMock(return_value={})

    with (
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
    ):
        result = await write_final_answer(state, config)

    meta = result["evidence_metadata"]
    assert meta["fallback_occurred"] is True


@pytest.mark.asyncio
async def test_write_final_answer_no_notebook_still_has_metadata():
    """Even without notebook_id, evidence_metadata is populated (with defaults)."""
    from open_notebook.graphs.ask import Strategy, write_final_answer

    state = {
        "question": "General question",
        "strategy": Strategy(reasoning="general", searches=[]),
        "answers": ["Answer."],
        "final_answer": "",
        "notebook_id": None,
        "candidate_source_id": None,
        "evidence_need": None,  # Not set
        "evidence_metadata": None,
    }

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=_make_mock_ai_message("Answer."))

    config = MagicMock()
    config.get = MagicMock(return_value={})

    with (
        patch("open_notebook.graphs.ask.provision_langchain_model", new=AsyncMock(return_value=mock_model)),
        patch("open_notebook.graphs.ask.extract_text_content", side_effect=lambda x: x),
        patch("open_notebook.graphs.ask.clean_thinking_content", side_effect=lambda x: x),
    ):
        result = await write_final_answer(state, config)

    meta = result["evidence_metadata"]
    assert meta["evidence_need"] == "overview"  # default
    assert "vector_search" in meta["evidence_layers_used"]
