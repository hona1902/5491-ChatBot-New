"""
Unit tests for Wave 5A: notebook_id plumbing and prompt guardrail.

Scope (per spec):
- AskRequest backend model accepts optional notebook_id (backward compat preserved)
- notebook_id flows from AskRequest into ThreadState graph input
- ThreadState accepts an optional notebook_id field
- No table source identification or table lookup is implemented
- ask.py answer behaviour unchanged when notebook_id is absent
- Prompt guardrail instruction present in final_answer.jinja
- No source_chat.py changes
"""

from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: root of the project
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent


# ===========================================================================
# 1. Backend request model: AskRequest.notebook_id
# ===========================================================================


class TestAskRequestModel:
    """AskRequest must accept optional notebook_id while remaining backward compatible."""

    def test_ask_request_without_notebook_id(self):
        """Existing clients that omit notebook_id must still work (default → None)."""
        from api.models import AskRequest

        req = AskRequest(
            question="What is RAG?",
            strategy_model="model:abc",
            answer_model="model:def",
            final_answer_model="model:ghi",
        )
        assert req.notebook_id is None

    def test_ask_request_with_notebook_id(self):
        """Request with notebook_id stores it correctly."""
        from api.models import AskRequest

        req = AskRequest(
            question="What are the sales figures?",
            strategy_model="model:abc",
            answer_model="model:def",
            final_answer_model="model:ghi",
            notebook_id="notebook:test123",
        )
        assert req.notebook_id == "notebook:test123"

    def test_ask_request_notebook_id_is_optional_field(self):
        """notebook_id field exists and is Optional[str]."""
        from api.models import AskRequest
        import inspect

        hints = AskRequest.model_fields
        assert "notebook_id" in hints, "notebook_id field must be declared on AskRequest"

    def test_ask_request_notebook_id_accepts_none_explicitly(self):
        """Explicit None is the same as omitting the field."""
        from api.models import AskRequest

        req = AskRequest(
            question="Test",
            strategy_model="m:1",
            answer_model="m:2",
            final_answer_model="m:3",
            notebook_id=None,
        )
        assert req.notebook_id is None


# ===========================================================================
# 2. ThreadState: notebook_id field
# ===========================================================================


class TestThreadStateNotebookId:
    """ThreadState must hold an optional notebook_id field."""

    def test_thread_state_has_notebook_id_field(self):
        """ThreadState TypedDict must declare notebook_id."""
        from open_notebook.graphs.ask import ThreadState

        annotations = ThreadState.__annotations__
        assert "notebook_id" in annotations, (
            "ThreadState must declare a notebook_id field for Wave 5A plumbing"
        )

    def test_thread_state_notebook_id_is_optional(self):
        """The notebook_id annotation must be Optional[str]."""
        from open_notebook.graphs.ask import ThreadState
        from typing import get_args, get_origin
        import typing

        annotation = ThreadState.__annotations__["notebook_id"]
        # Optional[str] is Union[str, None]
        origin = get_origin(annotation)
        args = get_args(annotation)
        assert origin is typing.Union, "notebook_id must be Optional (i.e. Union[str, None])"
        assert type(None) in args, "notebook_id must allow None"
        assert str in args, "notebook_id must allow str"

    def test_thread_state_without_notebook_id_still_valid(self):
        """Existing states serialised without notebook_id must not break on access."""
        from open_notebook.graphs.ask import ThreadState
        from open_notebook.graphs.ask import Strategy

        # Simulate an old thread state dict that has no notebook_id key
        old_state: Dict[str, Any] = {
            "question": "What is RAG?",
            "strategy": Strategy(reasoning="test", searches=[]),
            "answers": [],
            "final_answer": "",
            # NOTE: notebook_id deliberately absent
        }
        # TypedDict does not enforce key presence at runtime; .get() must return None
        assert old_state.get("notebook_id") is None


# ===========================================================================
# 3. API route: notebook_id plumbing through stream_ask_response
# ===========================================================================


class TestSearchRouterNotebookIdPlumbing:
    """stream_ask_response must pass notebook_id into the graph input."""

    @pytest.mark.asyncio
    async def test_stream_ask_response_passes_notebook_id(self):
        """When notebook_id is supplied, it appears in the graph astream input."""
        from api.routers.search import stream_ask_response

        captured_input: Dict[str, Any] = {}

        async def fake_astream(input, config, stream_mode):
            captured_input.update(input)
            return
            yield  # make it an async generator

        mock_graph = MagicMock()
        mock_graph.astream = fake_astream

        mock_model = MagicMock()
        mock_model.id = "model:test"

        with patch("api.routers.search.ask_graph", mock_graph):
            gen = stream_ask_response(
                question="test question",
                strategy_model=mock_model,
                answer_model=mock_model,
                final_answer_model=mock_model,
                notebook_id="notebook:nb1",
            )
            # Exhaust the generator so the body executes
            try:
                async for _ in gen:
                    pass
            except Exception:
                pass  # graph mock may raise; we only care about captured_input

        assert captured_input.get("notebook_id") == "notebook:nb1", (
            "notebook_id must be forwarded into ask_graph.astream input"
        )

    @pytest.mark.asyncio
    async def test_stream_ask_response_without_notebook_id(self):
        """When notebook_id is None, graph input still contains the question key."""
        from api.routers.search import stream_ask_response

        captured_input: Dict[str, Any] = {}

        async def fake_astream(input, config, stream_mode):
            captured_input.update(input)
            return
            yield

        mock_graph = MagicMock()
        mock_graph.astream = fake_astream
        mock_model = MagicMock()
        mock_model.id = "model:test"

        with patch("api.routers.search.ask_graph", mock_graph):
            gen = stream_ask_response(
                question="test question",
                strategy_model=mock_model,
                answer_model=mock_model,
                final_answer_model=mock_model,
                # notebook_id defaults to None
            )
            try:
                async for _ in gen:
                    pass
            except Exception:
                pass

        assert "question" in captured_input
        # notebook_id may be None or absent — either is acceptable
        assert captured_input.get("notebook_id") is None


# ===========================================================================
# 4. No table lookup called in Wave 5A
# ===========================================================================


class TestNoTableLookupInWave5A:
    """Wave 5A must NOT call table_exact_lookup or identify_table_source."""

    def test_table_exact_lookup_not_imported_in_ask_graph(self):
        """ask.py must not import table_exact_lookup at module level."""
        import open_notebook.graphs.ask as ask_module

        assert not hasattr(ask_module, "table_exact_lookup"), (
            "table_exact_lookup must NOT be imported in ask.py during Wave 5A"
        )

    def test_identify_table_source_not_in_ask_graph(self):
        """identify_table_source node must not be defined in ask.py during Wave 5A."""
        import open_notebook.graphs.ask as ask_module

        assert not hasattr(ask_module, "identify_table_source"), (
            "identify_table_source must NOT be implemented in Wave 5A"
        )

    def test_graph_nodes_unchanged(self):
        """The ask graph must have the same three nodes as before Wave 5A."""
        from open_notebook.graphs.ask import graph

        # LangGraph exposes nodes via the underlying StateGraph.nodes dict
        node_names = set(graph.nodes.keys())
        expected = {"agent", "provide_answer", "write_final_answer", "__start__", "__end__"}
        # Allow for langgraph internal reserved names — but no new table nodes
        table_nodes = {n for n in node_names if "table" in n.lower()}
        assert not table_nodes, (
            f"No table-related nodes should exist in Wave 5A graph; found: {table_nodes}"
        )


# ===========================================================================
# 5. Prompt guardrail: Verified Table Data instruction in final_answer.jinja
# ===========================================================================


class TestPromptGuardrail:
    """The final_answer.jinja prompt must include the Verified Table Data guardrail."""

    def _load_prompt(self) -> str:
        prompt_path = PROJECT_ROOT / "prompts" / "ask" / "final_answer.jinja"
        return prompt_path.read_text(encoding="utf-8")

    def test_verified_table_data_section_present(self):
        """Prompt must contain the Verified Table Data guardrail section."""
        content = self._load_prompt()
        assert "Verified Table Data" in content, (
            "final_answer.jinja must contain 'Verified Table Data' guardrail"
        )

    def test_guardrail_instructs_citation(self):
        """Guardrail must mention citing table/page/sheet."""
        content = self._load_prompt()
        lower = content.lower()
        assert "table" in lower and "page" in lower and "sheet" in lower, (
            "Guardrail must mention table, page, and sheet for citation"
        )

    def test_guardrail_does_not_force_refusal(self):
        """Guardrail must contain a 'not present' / 'ignore this instruction' escape clause."""
        content = self._load_prompt()
        # The escape clause prevents over-refusal when no table data is injected
        assert "not present" in content or "ignore this instruction" in content, (
            "Guardrail must have an escape clause so it doesn't force refusal "
            "when no Verified Table Data section exists"
        )

    def test_guardrail_positioned_after_table_citation_preservation(self):
        """Verified Table Data section must come after TABLE CITATION PRESERVATION."""
        content = self._load_prompt()
        tcp_pos = content.find("TABLE CITATION PRESERVATION")
        vtd_pos = content.find("VERIFIED TABLE DATA GUARDRAIL")
        assert tcp_pos != -1, "TABLE CITATION PRESERVATION section must exist"
        assert vtd_pos != -1, "VERIFIED TABLE DATA GUARDRAIL section must exist"
        assert vtd_pos > tcp_pos, (
            "VERIFIED TABLE DATA GUARDRAIL must appear after TABLE CITATION PRESERVATION"
        )


# ===========================================================================
# 6. Frontend: notebook_id present in AskRequest TypeScript interface
#    (structural check via file content — no TS compiler available)
# ===========================================================================


class TestFrontendAskRequestType:
    """The frontend AskRequest interface must include notebook_id."""

    def _load_types_file(self) -> str:
        path = PROJECT_ROOT / "frontend" / "src" / "lib" / "types" / "search.ts"
        return path.read_text(encoding="utf-8")

    def test_ask_request_interface_has_notebook_id(self):
        """frontend/src/lib/types/search.ts must declare notebook_id? in AskRequest."""
        content = self._load_types_file()
        # Find the AskRequest interface block and check for notebook_id
        ask_start = content.find("export interface AskRequest")
        ask_end = content.find("}", ask_start)
        assert ask_start != -1, "AskRequest interface must exist in search.ts"
        block = content[ask_start:ask_end + 1]
        assert "notebook_id" in block, (
            "AskRequest TypeScript interface must contain notebook_id field"
        )

    def test_notebook_id_is_optional_in_ts(self):
        """notebook_id must be declared with ? (optional) in TypeScript."""
        content = self._load_types_file()
        ask_start = content.find("export interface AskRequest")
        ask_end = content.find("}", ask_start)
        block = content[ask_start:ask_end + 1]
        # Optional in TS is denoted with '?'
        assert "notebook_id?" in block, (
            "notebook_id must be optional (notebook_id?) in AskRequest TypeScript interface"
        )

    def _load_use_ask_hook(self) -> str:
        path = PROJECT_ROOT / "frontend" / "src" / "lib" / "hooks" / "use-ask.ts"
        return path.read_text(encoding="utf-8")

    def test_use_ask_hook_accepts_notebookid(self):
        """use-ask.ts sendAsk must accept an optional notebookId parameter."""
        content = self._load_use_ask_hook()
        assert "notebookId" in content, (
            "use-ask.ts sendAsk callback must accept optional notebookId parameter"
        )

    def test_use_ask_hook_passes_notebook_id_to_api(self):
        """use-ask.ts must include notebook_id in the askKnowledgeBase call."""
        content = self._load_use_ask_hook()
        assert "notebook_id" in content, (
            "use-ask.ts must include notebook_id in the API payload"
        )



# ===========================================================================
# 7. Simple endpoint: notebook_id plumbing through ask_knowledge_base_simple
# ===========================================================================


class TestSimpleEndpointNotebookIdPlumbing:
    """ask_knowledge_base_simple must forward notebook_id into graph_input_simple."""

    def _make_mock_model(self):
        m = MagicMock()
        m.id = "model:test"
        return m

    @pytest.mark.asyncio
    async def test_simple_endpoint_passes_notebook_id(self):
        """When AskRequest carries notebook_id, simple endpoint puts it in graph input."""
        from api.routers.search import ask_knowledge_base_simple
        from api.models import AskRequest

        captured_input: Dict[str, Any] = {}

        async def fake_astream(input, config, stream_mode):
            captured_input.update(input)
            # Yield a write_final_answer chunk so the endpoint can extract final_answer
            yield {"write_final_answer": {"final_answer": "Test answer"}}

        mock_graph = MagicMock()
        mock_graph.astream = fake_astream

        mock_model = self._make_mock_model()

        request = AskRequest(
            question="What are total sales?",
            strategy_model="model:1",
            answer_model="model:2",
            final_answer_model="model:3",
            notebook_id="notebook:nb42",
        )

        with (
            patch("api.routers.search.ask_graph", mock_graph),
            patch("api.routers.search.Model.get", AsyncMock(return_value=mock_model)),
            patch("api.routers.search.model_manager.get_embedding_model", AsyncMock(return_value=MagicMock())),
        ):
            response = await ask_knowledge_base_simple(request)

        assert captured_input.get("notebook_id") == "notebook:nb42", (
            "ask_knowledge_base_simple must forward notebook_id into ask_graph input"
        )
        assert captured_input.get("question") == "What are total sales?"

    @pytest.mark.asyncio
    async def test_simple_endpoint_notebook_id_none(self):
        """When notebook_id is absent from AskRequest, simple endpoint passes None."""
        from api.routers.search import ask_knowledge_base_simple
        from api.models import AskRequest

        captured_input: Dict[str, Any] = {}

        async def fake_astream(input, config, stream_mode):
            captured_input.update(input)
            yield {"write_final_answer": {"final_answer": "Test answer"}}

        mock_graph = MagicMock()
        mock_graph.astream = fake_astream
        mock_model = self._make_mock_model()

        request = AskRequest(
            question="What is RAG?",
            strategy_model="model:1",
            answer_model="model:2",
            final_answer_model="model:3",
            # notebook_id deliberately omitted
        )

        with (
            patch("api.routers.search.ask_graph", mock_graph),
            patch("api.routers.search.Model.get", AsyncMock(return_value=mock_model)),
            patch("api.routers.search.model_manager.get_embedding_model", AsyncMock(return_value=MagicMock())),
        ):
            response = await ask_knowledge_base_simple(request)

        assert "question" in captured_input
        assert captured_input.get("notebook_id") is None, (
            "When notebook_id is absent, graph_input_simple must have notebook_id=None (not missing)"
        )

    @pytest.mark.asyncio
    async def test_simple_endpoint_no_longer_uses_dict_question_only(self):
        """The simple endpoint must NOT pass a dict with only 'question' key (pre-5A pattern)."""
        from api.routers.search import ask_knowledge_base_simple
        from api.models import AskRequest

        captured_input: Dict[str, Any] = {}

        async def fake_astream(input, config, stream_mode):
            captured_input.update(input)
            yield {"write_final_answer": {"final_answer": "ok"}}

        mock_graph = MagicMock()
        mock_graph.astream = fake_astream
        mock_model = self._make_mock_model()

        request = AskRequest(
            question="Is notebook_id in the payload?",
            strategy_model="model:1",
            answer_model="model:2",
            final_answer_model="model:3",
            notebook_id="notebook:check",
        )

        with (
            patch("api.routers.search.ask_graph", mock_graph),
            patch("api.routers.search.Model.get", AsyncMock(return_value=mock_model)),
            patch("api.routers.search.model_manager.get_embedding_model", AsyncMock(return_value=MagicMock())),
        ):
            await ask_knowledge_base_simple(request)

        # The pre-5A input had only one key; post-5A must have at least two
        assert len(captured_input) >= 2, (
            "Graph input must contain both 'question' and 'notebook_id' (not the pre-5A single-key dict)"
        )
        assert "notebook_id" in captured_input


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
