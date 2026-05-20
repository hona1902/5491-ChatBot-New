"""
tests/test_evidence_prompts.py
===============================
Prompt snapshot tests for Evidence v2 hierarchy and guardrails.

Verifies that the key evidence-related sections are present in the
rendered Jinja templates used by the ask graph.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers — read template files directly
# ---------------------------------------------------------------------------


def _read_template(name: str) -> str:
    """Read a Jinja template file from the prompts directory."""
    path = Path(__file__).resolve().parent.parent / "prompts" / "ask" / name
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# query_process.jinja tests
# ---------------------------------------------------------------------------


class TestQueryProcessPrompt:
    """Verify evidence hierarchy content in query_process.jinja."""

    @pytest.fixture(autouse=True)
    def _load_template(self):
        self.template = _read_template("query_process.jinja")

    def test_has_evidence_hierarchy_section(self):
        assert "EVIDENCE HIERARCHY" in self.template

    def test_has_four_tiers(self):
        assert "Verified Table Data" in self.template
        assert "Full Source Evidence" in self.template
        assert "Retrieved Results" in self.template
        assert "Transformation Output" in self.template

    def test_has_conflict_resolution(self):
        assert "Conflict Resolution" in self.template

    def test_has_table_grounding_rules(self):
        assert "TABLE GROUNDING RULES" in self.template

    def test_transformer_guardrail_present(self):
        """Transformation Output must explicitly say NEVER override higher tiers."""
        assert "NEVER override" in self.template

    def test_priority_order_correct(self):
        """Verified Table Data should appear before Full Source Evidence in hierarchy."""
        vtd_pos = self.template.index("1. **Verified Table Data**")
        fse_pos = self.template.index("2. **Full Source Evidence**")
        rr_pos = self.template.index("3. **Retrieved Results**")
        to_pos = self.template.index("4. **Transformation Output**")
        assert vtd_pos < fse_pos < rr_pos < to_pos


# ---------------------------------------------------------------------------
# final_answer.jinja tests
# ---------------------------------------------------------------------------


class TestFinalAnswerPrompt:
    """Verify evidence hierarchy content in final_answer.jinja."""

    @pytest.fixture(autouse=True)
    def _load_template(self):
        self.template = _read_template("final_answer.jinja")

    def test_has_evidence_hierarchy_section(self):
        assert "EVIDENCE HIERARCHY" in self.template

    def test_has_four_tiers(self):
        assert "Verified Table Data" in self.template
        assert "Full Source Evidence" in self.template
        assert "Retrieved Results" in self.template
        assert "Transformation Output" in self.template

    def test_has_conflict_resolution(self):
        assert "Conflict Resolution" in self.template

    def test_has_verified_table_data_guardrail(self):
        assert "VERIFIED TABLE DATA GUARDRAIL" in self.template

    def test_transformer_guardrail_present(self):
        assert "NEVER override" in self.template

    def test_has_escape_clause(self):
        """When no Verified Table Data is present, LLM should not mention absence."""
        assert "ignore this instruction entirely" in self.template

    def test_guardrail_prioritises_verified_data(self):
        """Guardrail explicitly says to prefer Verified Table Data."""
        assert "Prefer values from Verified Table Data" in self.template

    def test_do_not_fabricate(self):
        """Should explicitly instruct not to fabricate from lower tiers."""
        assert "Do NOT fabricate" in self.template


# ---------------------------------------------------------------------------
# entry.jinja tests
# ---------------------------------------------------------------------------


class TestEntryPrompt:
    """Verify evidence_need classification in entry.jinja."""

    @pytest.fixture(autouse=True)
    def _load_template(self):
        self.template = _read_template("entry.jinja")

    def test_has_evidence_need_field(self):
        assert "evidence_need" in self.template

    def test_has_overview_tier(self):
        assert "overview" in self.template

    def test_has_factual_tier(self):
        assert "factual" in self.template

    def test_has_legal_comparison_tier(self):
        assert "legal_comparison" in self.template

    def test_has_classification_guidance(self):
        """Template should explain what each evidence tier means."""
        assert "table" in self.template.lower() or "specific value" in self.template.lower()
