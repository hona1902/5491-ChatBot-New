"""Tests for Evidence v2: Strategy model evidence_need + heuristic fallback."""

import pytest

from open_notebook.graphs.ask import (
    EvidenceNeed,
    Strategy,
    _EVIDENCE_SIGNAL_KEYWORDS,
    _heuristic_evidence_upgrade,
)


# ── Strategy model parsing ────────────────────────────────────────────────


class TestStrategyEvidenceNeed:
    """Verify Strategy model parses evidence_need correctly."""

    def test_parse_with_overview(self):
        s = Strategy(
            reasoning="test",
            searches=[],
            evidence_need="overview",
        )
        assert s.evidence_need == "overview"

    def test_parse_with_factual(self):
        s = Strategy(
            reasoning="test",
            searches=[],
            evidence_need="factual",
        )
        assert s.evidence_need == "factual"

    def test_parse_with_legal_comparison(self):
        s = Strategy(
            reasoning="test",
            searches=[],
            evidence_need="legal_comparison",
        )
        assert s.evidence_need == "legal_comparison"

    def test_default_is_overview(self):
        """Missing evidence_need defaults to 'overview' for backward compat."""
        s = Strategy(reasoning="test", searches=[])
        assert s.evidence_need == "overview"

    def test_from_dict_without_evidence_need(self):
        """Simulates old checkpoint data that has no evidence_need."""
        data = {"reasoning": "test", "searches": []}
        s = Strategy(**data)
        assert s.evidence_need == "overview"

    def test_invalid_evidence_need_raises(self):
        """Invalid literal values should be rejected by Pydantic."""
        with pytest.raises(Exception):
            Strategy(
                reasoning="test",
                searches=[],
                evidence_need="invalid_tier",  # type: ignore[arg-type]
            )


# ── Heuristic fallback ────────────────────────────────────────────────────


class TestHeuristicEvidenceUpgrade:
    """Verify keyword-based fallback upgrades overview → factual."""

    # -- Upgrades --

    def test_upgrade_on_english_table_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "What is in the table?")
        assert result == "factual"

    def test_upgrade_on_english_clause_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "Find clause 5.2")
        assert result == "factual"

    def test_upgrade_on_english_amendment_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "What was the amendment?")
        assert result == "factual"

    def test_upgrade_on_english_rate_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "What is the interest rate?")
        assert result == "factual"

    def test_upgrade_on_english_comparison_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "Compare the two versions")
        assert result == "factual"

    def test_upgrade_on_english_column_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "What column has the data?")
        assert result == "factual"

    def test_upgrade_on_vietnamese_bang_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "Xem bảng lãi suất")
        assert result == "factual"

    def test_upgrade_on_vietnamese_dieu_khoan_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "Nội dung điều khoản 3")
        assert result == "factual"

    def test_upgrade_on_vietnamese_so_sanh_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "So sánh hai phiên bản")
        assert result == "factual"

    def test_upgrade_on_vietnamese_phu_luc_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "Phụ lục A có gì?")
        assert result == "factual"

    def test_upgrade_on_vietnamese_sua_doi_keyword(self):
        result = _heuristic_evidence_upgrade("overview", "Điểm sửa đổi bổ sung")
        assert result == "factual"

    def test_upgrade_case_insensitive(self):
        result = _heuristic_evidence_upgrade("overview", "Show the TABLE data")
        assert result == "factual"

    # -- No upgrade --

    def test_no_upgrade_for_general_question(self):
        result = _heuristic_evidence_upgrade("overview", "What is this document about?")
        assert result == "overview"

    def test_no_upgrade_for_summary_question(self):
        result = _heuristic_evidence_upgrade("overview", "Summarize the main points")
        assert result == "overview"

    # -- Never downgrades --

    def test_never_downgrade_factual(self):
        """Factual should stay factual even without keywords."""
        result = _heuristic_evidence_upgrade("factual", "Summarize the main points")
        assert result == "factual"

    def test_never_downgrade_legal_comparison(self):
        """legal_comparison should stay even without keywords."""
        result = _heuristic_evidence_upgrade(
            "legal_comparison", "What is this about?"
        )
        assert result == "legal_comparison"

    def test_factual_with_keywords_stays_factual(self):
        """Already factual + keywords = still factual (not upgraded further)."""
        result = _heuristic_evidence_upgrade("factual", "Show the table")
        assert result == "factual"


# ── Signal keywords sanity check ──────────────────────────────────────────


class TestSignalKeywords:
    """Verify the keyword set is well-formed."""

    def test_keywords_not_empty(self):
        assert len(_EVIDENCE_SIGNAL_KEYWORDS) > 0

    def test_keywords_are_lowercase(self):
        for kw in _EVIDENCE_SIGNAL_KEYWORDS:
            assert kw == kw.lower(), f"Keyword '{kw}' is not lowercase"

    def test_contains_both_english_and_vietnamese(self):
        # At least one English and one Vietnamese keyword
        assert "table" in _EVIDENCE_SIGNAL_KEYWORDS
        assert "bảng" in _EVIDENCE_SIGNAL_KEYWORDS
