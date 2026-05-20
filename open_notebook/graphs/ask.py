import operator
from typing import Annotated, List, Literal, Optional

import numpy as np
from ai_prompter import Prompter
from langchain_core.output_parsers.pydantic import PydanticOutputParser
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from loguru import logger
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from open_notebook.ai.provision import provision_langchain_model
from open_notebook.config import ASK_TABLE_SOURCE_THRESHOLD
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import vector_search
from open_notebook.exceptions import OpenNotebookError
from open_notebook.utils import clean_thinking_content
from open_notebook.utils.error_classifier import classify_error
from open_notebook.utils.text_utils import extract_text_content


class SubGraphState(TypedDict):
    question: str
    term: str
    instructions: str
    results: dict
    answer: str
    ids: list  # Added for provide_answer function
    # Wave 5C: candidate table source for this sub-search.
    # When not None, provide_answer will call table_exact_lookup and prepend
    # any result as a '## Verified Table Data' section.
    # None = no table source identified → normal prose QA only.
    candidate_source_id: Optional[str]
    # Evidence v2: evidence need tier for conditional full-content fetch.
    evidence_need: Optional[str]
    # Evidence v2: notebook_id for scoping full-content queries.
    notebook_id: Optional[str]


class Search(BaseModel):
    term: str
    instructions: str = Field(
        description="Tell the answeting LLM what information you need extracted from this search"
    )


# Evidence need tiers for notebook QA evidence routing.
# - overview: insight summaries are sufficient
# - factual: full source content and/or table data required
# - legal_comparison: full content required with clause/page/table citations
EvidenceNeed = Literal["overview", "factual", "legal_comparison"]


class Strategy(BaseModel):
    reasoning: str
    searches: List[Search] = Field(
        default_factory=list,
        description="You can add up to five searches to this strategy",
    )
    evidence_need: EvidenceNeed = Field(
        default="overview",
        description=(
            "Classify the question's evidence requirement: "
            "'overview' for summary questions, "
            "'factual' for table/number/specific-value questions, "
            "'legal_comparison' for amendment/clause/change-comparison questions"
        ),
    )


# Evidence v2: metadata model exposed in the API response.
class EvidenceMetadata(BaseModel):
    """Metadata about the evidence routing path taken for this Q&A cycle."""
    evidence_need: EvidenceNeed = "overview"
    evidence_layers_used: List[str] = Field(
        default_factory=list,
        description="List of evidence layers that contributed: "
        "'verified_table_data', 'full_source_evidence', 'vector_search'",
    )
    fallback_occurred: bool = Field(
        default=False,
        description="True if a heuristic or tier upgrade changed the evidence path",
    )


class ThreadState(TypedDict):
    question: str
    strategy: Strategy
    answers: Annotated[list, operator.add]
    final_answer: str
    # Wave 5A: notebook context plumbing — optional, backward compatible.
    # Existing saved states without this field still deserialize correctly
    # because TypedDict fields are not enforced at runtime.
    notebook_id: Optional[str]  # Notebook scope for future table-aware QA
    # Wave 5B: set by identify_table_source; None = no table source found.
    candidate_source_id: Optional[str]
    # Evidence v2: classified evidence need for the question.
    # Flows from Strategy model → ThreadState → downstream nodes.
    # Defaults to "overview" when absent (backward compatible).
    evidence_need: Optional[EvidenceNeed]
    # Evidence v2: metadata about evidence routing (populated by write_final_answer).
    evidence_metadata: Optional[dict]

# ---------------------------------------------------------------------------
# Evidence v2: heuristic keyword fallback for evidence classification.
# If the LLM returns evidence_need = "overview" but the question contains
# high-precision signal words, upgrade to "factual".  Never downgrades.
# ---------------------------------------------------------------------------
_EVIDENCE_SIGNAL_KEYWORDS: frozenset[str] = frozenset({
    # English — domain-specific terms only.
    # Avoid overly common words (article, section, paragraph, rate, schedule)
    # that would false-positive on general/summary questions.
    "table", "column", "row", "cell", "clause", "amendment", "amend",
    "compare", "comparison", "appendix", "annex",
    "fee schedule", "interest rate",
    # Vietnamese — these are domain-specific in banking/legal context
    "bảng", "cột", "hàng", "ô", "điều khoản",
    "sửa đổi", "bổ sung", "so sánh", "phụ lục", "biểu", "lãi suất",
    "phí",
})


def _heuristic_evidence_upgrade(
    evidence_need: EvidenceNeed, question: str
) -> EvidenceNeed:
    """Upgrade evidence_need from 'overview' to 'factual' if signal keywords found.

    Never downgrades: if the LLM already set 'factual' or 'legal_comparison',
    the heuristic does not override.
    """
    if evidence_need != "overview":
        return evidence_need

    q_lower = question.lower()
    for keyword in _EVIDENCE_SIGNAL_KEYWORDS:
        if keyword in q_lower:
            logger.debug(
                f"heuristic_evidence_upgrade: keyword '{keyword}' found — "
                f"upgrading overview → factual"
            )
            return "factual"
    return "overview"


async def call_model_with_messages(state: ThreadState, config: RunnableConfig) -> dict:
    try:
        parser = PydanticOutputParser(pydantic_object=Strategy)
        system_prompt = Prompter(prompt_template="ask/entry", parser=parser).render(  # type: ignore[arg-type]
            data=state  # type: ignore[arg-type]
        )
        model = await provision_langchain_model(
            system_prompt,
            config.get("configurable", {}).get("strategy_model"),
            "tools",
            max_tokens=2000,
            structured=dict(type="json"),
        )
        # model = model.bind_tools(tools)
        # First get the raw response from the model
        ai_message = await model.ainvoke(system_prompt)

        # Clean the thinking content from the response
        message_content = extract_text_content(ai_message.content)
        cleaned_content = clean_thinking_content(message_content)

        # Parse the cleaned JSON content
        strategy = parser.parse(cleaned_content)

        # Evidence v2: extract evidence_need from strategy and apply heuristic.
        raw_need: EvidenceNeed = strategy.evidence_need
        question: str = state.get("question", "")  # type: ignore[assignment]
        final_need = _heuristic_evidence_upgrade(raw_need, question)

        if final_need != raw_need:
            logger.info(
                f"evidence_need upgraded from '{raw_need}' to '{final_need}' "
                f"by heuristic fallback"
            )

        return {"strategy": strategy, "evidence_need": final_need}

    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


# ---------------------------------------------------------------------------
# Evidence v2: identify_table_source (expanded from Wave 5B)
# ---------------------------------------------------------------------------
# Previously restricted to CSV/XLSX via _source_is_tabular filter.
# Now eligible for ANY source type that has source_table records
# (including DOCX and PDF with extracted tables).


async def identify_table_source(state: ThreadState, config: RunnableConfig) -> dict:
    """
    Identify the single most relevant source in the notebook that has
    structured table data (source_table records) for use as a candidate.

    Evidence v2 change: eligibility is no longer limited to CSV/XLSX.
    Any source type (CSV, XLSX, DOCX, PDF) with source_table records
    is now eligible for table lookup.

    Rules (fail-closed throughout):
    1. notebook_id missing  → candidate_source_id = None.
    2. Zero sources with source_table records → None.
    3. Exactly one candidate    → selected directly (no embedding call).
    4. Multiple candidates      → embed question once, pick best by cosine
                                   similarity to source metadata (title/topics);
                                   tie-break by source_table count.
                                   If best score < ASK_TABLE_SOURCE_THRESHOLD → None.
    5. Any error                → None (ask graph continues normal prose QA).

    Does NOT call table_exact_lookup or inject Verified Table Data.
    """
    notebook_id: Optional[str] = state.get("notebook_id")  # type: ignore[assignment]
    if not notebook_id:
        logger.debug("identify_table_source: no notebook_id — skipping.")
        return {"candidate_source_id": None}

    try:
        # ------------------------------------------------------------------ #
        # 1. Query sources linked to this notebook via reference edges.       #
        # ------------------------------------------------------------------ #
        nb_rid = ensure_record_id(notebook_id)

        nb_sources_raw = await repo_query(
            """
            SELECT in AS source FROM reference WHERE out = $nb_id FETCH source
            """,
            {"nb_id": nb_rid},
        )
    except Exception as exc:
        logger.warning(
            f"identify_table_source: DB error querying notebook sources "
            f"(notebook_id={notebook_id}): {exc} — failing closed."
        )
        return {"candidate_source_id": None}

    try:
        # ------------------------------------------------------------------ #
        # 2. Collect ALL sources (no file-type filter).                       #
        # ------------------------------------------------------------------ #
        all_sources: list[dict] = []

        for row in nb_sources_raw or []:
            src = row.get("source") or {}
            if not isinstance(src, dict):
                continue

            source_id = src.get("id") or ""
            if not source_id:
                continue

            asset = src.get("asset") or {}
            file_path = asset.get("file_path", "") if isinstance(asset, dict) else ""

            all_sources.append(
                {
                    "source_id": str(source_id),
                    "title": src.get("title") or "",
                    "topics": src.get("topics") or [],
                    "file_path": file_path,
                }
            )

        if not all_sources:
            logger.debug(
                f"identify_table_source: no sources in notebook {notebook_id}."
            )
            return {"candidate_source_id": None}

        # ------------------------------------------------------------------ #
        # 3. Keep only sources that have at least one source_table record.    #
        #    This is the ONLY filter — any source type is eligible.           #
        # ------------------------------------------------------------------ #
        candidates_with_tables: list[dict] = []
        for cand in all_sources:
            try:
                count_result = await repo_query(
                    "SELECT count() AS cnt FROM source_table "
                    "WHERE source = $src_id GROUP ALL",
                    {"src_id": ensure_record_id(cand["source_id"])},
                )
                cnt = count_result[0]["cnt"] if count_result else 0
                if cnt > 0:
                    cand["table_count"] = cnt
                    candidates_with_tables.append(cand)
            except Exception as exc:
                logger.debug(
                    f"identify_table_source: error counting source_table for "
                    f"{cand['source_id']}: {exc} — skipping candidate."
                )
                continue

        if not candidates_with_tables:
            logger.debug(
                f"identify_table_source: no sources with source_table "
                f"records in notebook {notebook_id}."
            )
            return {"candidate_source_id": None}


        # ------------------------------------------------------------------ #
        # 4a. Fast path: exactly one candidate.                               #
        # ------------------------------------------------------------------ #
        if len(candidates_with_tables) == 1:
            chosen_id = candidates_with_tables[0]["source_id"]
            logger.info(
                f"identify_table_source: single CSV/XLSX candidate {chosen_id} "
                f"selected without embedding (notebook={notebook_id})."
            )
            return {"candidate_source_id": chosen_id}

        # ------------------------------------------------------------------ #
        # 4b. Multiple candidates → embed question, score against metadata.  #
        # ------------------------------------------------------------------ #
        question: str = state.get("question", "")  # type: ignore[assignment]
        try:
            from open_notebook.utils.embedding import generate_embedding

            query_vec = np.array(
                await generate_embedding(question), dtype=np.float64
            )
            q_norm = np.linalg.norm(query_vec)
            if q_norm > 0:
                query_vec = query_vec / q_norm
            else:
                logger.debug(
                    "identify_table_source: zero-norm query embedding — "
                    "falling back to first candidate."
                )
                return {"candidate_source_id": candidates_with_tables[0]["source_id"]}
        except Exception as exc:
            logger.warning(
                f"identify_table_source: embedding error for question "
                f"({exc!r}) — failing closed."
            )
            return {"candidate_source_id": None}

        # Build a text blob per candidate from available metadata.
        scored: list[tuple[float, dict]] = []
        for cand in candidates_with_tables:
            meta_parts = [cand["title"]]
            topics = cand.get("topics") or []
            if topics:
                meta_parts.append(" ".join(str(t) for t in topics))
            meta_text = " ".join(p for p in meta_parts if p).strip()
            if not meta_text:
                meta_text = cand["file_path"]

            try:
                from open_notebook.utils.embedding import generate_embedding

                cand_vec = np.array(
                    await generate_embedding(meta_text), dtype=np.float64
                )
                c_norm = np.linalg.norm(cand_vec)
                if c_norm == 0:
                    continue
                cand_vec = cand_vec / c_norm
                score = float(np.dot(query_vec, cand_vec))
            except Exception as exc:
                logger.debug(
                    f"identify_table_source: embedding error for candidate "
                    f"{cand['source_id']}: {exc} — skipping."
                )
                continue

            scored.append((score, cand))

        if not scored:
            logger.warning(
                "identify_table_source: all candidate embeddings failed — failing closed."
            )
            return {"candidate_source_id": None}

        # Sort by score desc, tie-break by table_count desc.
        scored.sort(key=lambda x: (x[0], x[1].get("table_count", 0)), reverse=True)
        best_score, best_cand = scored[0]

        # Threshold gate.
        threshold = max(0.0, min(1.0, ASK_TABLE_SOURCE_THRESHOLD))
        if best_score < threshold:
            logger.info(
                f"identify_table_source: best score {best_score:.3f} < threshold "
                f"{threshold} — no candidate selected (notebook={notebook_id})."
            )
            return {"candidate_source_id": None}

        logger.info(
            f"identify_table_source: selected {best_cand['source_id']} "
            f"(score={best_score:.3f}, threshold={threshold}, "
            f"notebook={notebook_id})."
        )
        return {"candidate_source_id": best_cand["source_id"]}

    except Exception as exc:
        logger.warning(
            f"identify_table_source: unexpected error — failing closed: {exc!r}"
        )
        return {"candidate_source_id": None}


async def trigger_queries(state: ThreadState, config: RunnableConfig):
    candidate_source_id: Optional[str] = state.get("candidate_source_id")  # type: ignore[assignment]
    evidence_need: Optional[str] = state.get("evidence_need")  # type: ignore[assignment]
    notebook_id: Optional[str] = state.get("notebook_id")  # type: ignore[assignment]
    return [
        Send(
            "provide_answer",
            {
                "question": state["question"],
                "instructions": s.instructions,
                "term": s.term,
                "candidate_source_id": candidate_source_id,
                # Evidence v2: pass evidence tier and notebook scope.
                "evidence_need": evidence_need,
                "notebook_id": notebook_id,
            },
        )
        for s in state["strategy"].searches
    ]

# ---------------------------------------------------------------------------
# Evidence v2: fetch_full_content helper
# ---------------------------------------------------------------------------


async def fetch_full_content(
    notebook_id: Optional[str],
    max_chars: Optional[int] = None,
) -> Optional[str]:
    """Fetch full_text for sources linked to *notebook_id*, up to *max_chars*.

    Returns a formatted string with source titles and content, or ``None``
    on error / empty results.  Truncation respects paragraph boundaries
    and appends a ``[TRUNCATED]`` marker.

    This is only called when ``evidence_need in {'factual', 'legal_comparison'}``
    — never for ``overview`` questions.
    """
    if not notebook_id:
        return None

    if max_chars is None:
        from open_notebook.config import EVIDENCE_FULL_TEXT_MAX_CHARS
        max_chars = EVIDENCE_FULL_TEXT_MAX_CHARS

    try:
        nb_rid = ensure_record_id(notebook_id)
        sources_raw = await repo_query(
            """
            SELECT in AS source FROM reference
            WHERE out = $nb_id FETCH source
            """,
            {"nb_id": nb_rid},
        )
    except Exception as exc:
        logger.warning(
            f"fetch_full_content: DB error querying sources for "
            f"notebook {notebook_id}: {exc}"
        )
        return None

    if not sources_raw:
        return None

    parts: list[str] = []
    total_chars = 0

    for row in sources_raw:
        src = row.get("source") or {}
        if not isinstance(src, dict):
            continue

        full_text = src.get("full_text") or ""
        if not full_text:
            continue

        title = src.get("title") or "Untitled Source"
        remaining = max_chars - total_chars
        if remaining <= 0:
            break

        if len(full_text) > remaining:
            # Truncate at the last paragraph boundary before the limit.
            truncated = full_text[:remaining]
            last_para = truncated.rfind("\n\n")
            if last_para > 0:
                truncated = truncated[:last_para]
            truncated += "\n\n[TRUNCATED — content exceeds character limit]"
            parts.append(f"### {title}\n\n{truncated}")
            total_chars += len(truncated)
            break
        else:
            parts.append(f"### {title}\n\n{full_text}")
            total_chars += len(full_text)

    if not parts:
        return None

    return "\n\n---\n\n".join(parts)


async def provide_answer(state: SubGraphState, config: RunnableConfig) -> dict:
    try:
        payload = state
        # if state["type"] == "text":
        #     results = text_search(state["term"], 10, True, True)
        # else:
        results = await vector_search(state["term"], 10, True, True)
        if len(results) == 0:
            return {"answers": []}
        payload["results"] = results
        ids = [r["id"] for r in results]
        payload["ids"] = ids

        # ------------------------------------------------------------------ #
        # Wave 5C: Verified Table Data injection.                             #
        # If a candidate tabular source was identified by identify_table_source
        # (Wave 5B), perform a scoped table_exact_lookup against that source.
        # On success, prepend the result as a ## Verified Table Data section
        # so the LLM prompt guardrail in query_process.jinja / final_answer.jinja
        # can prioritise it.  Fail-closed on every error path.
        # ------------------------------------------------------------------ #
        candidate_source_id: Optional[str] = state.get("candidate_source_id")  # type: ignore[assignment]
        verified_table_prefix: str = ""
        if candidate_source_id:
            try:
                # Lazy import so module-level namespace is never polluted.
                # (test_ask_wave5a asserts table_exact_lookup is NOT in ask module)
                from open_notebook.utils.table_lookup import (
                    table_exact_lookup as _table_exact_lookup,
                )

                question: str = state.get("question", "")  # type: ignore[assignment]
                lookup_result: Optional[str] = await _table_exact_lookup(
                    question, candidate_source_id
                )
                if lookup_result:
                    verified_table_prefix = (
                        "## Verified Table Data\n\n"
                        f"{lookup_result}\n\n"
                    )
                    logger.info(
                        f"provide_answer: injected Verified Table Data "
                        f"from source {candidate_source_id} "
                        f"({len(lookup_result)} chars)"
                    )
                else:
                    logger.debug(
                        f"provide_answer: table_exact_lookup returned None for "
                        f"source {candidate_source_id} — proceeding with prose QA only."
                    )
            except Exception as lookup_exc:
                # Fail-closed: any lookup error → no injection → normal QA.
                logger.warning(
                    f"provide_answer: table_exact_lookup error for source "
                    f"{candidate_source_id}: {lookup_exc!r} — proceeding with prose QA only."
                )

        # ------------------------------------------------------------------ #
        # Evidence v2: Full Source Evidence injection.                         #
        # When evidence_need is 'factual' or 'legal_comparison', fetch the    #
        # full_text from relevant sources and inject as                        #
        # '## Full Source Evidence'.  Skipped for 'overview' (no extra DB     #
        # query).  Fail-closed on errors.                                     #
        # ------------------------------------------------------------------ #
        evidence_need: str = state.get("evidence_need") or "overview"  # type: ignore[assignment]
        full_source_prefix: str = ""
        if evidence_need in ("factual", "legal_comparison"):
            nb_id: Optional[str] = state.get("notebook_id")  # type: ignore[assignment]
            try:
                full_content = await fetch_full_content(nb_id)
                if full_content:
                    full_source_prefix = (
                        "## Full Source Evidence\n\n"
                        f"{full_content}\n\n"
                    )
                    logger.info(
                        f"provide_answer: injected Full Source Evidence "
                        f"({len(full_content)} chars) for evidence_need={evidence_need}"
                    )
            except Exception as fc_exc:
                # Fail-closed: error → no injection → normal QA.
                logger.warning(
                    f"provide_answer: fetch_full_content error: {fc_exc!r} "
                    f"— proceeding without full source evidence."
                )

        # Build the rendered prompt, optionally prepending evidence sections.
        # Priority: Verified Table Data > Full Source Evidence > normal context.
        system_prompt = Prompter(prompt_template="ask/query_process").render(data=payload)  # type: ignore[arg-type]
        if full_source_prefix:
            system_prompt = full_source_prefix + system_prompt
        if verified_table_prefix:
            system_prompt = verified_table_prefix + system_prompt

        model = await provision_langchain_model(
            system_prompt,
            config.get("configurable", {}).get("answer_model"),
            "tools",
            max_tokens=2000,
        )
        ai_message = await model.ainvoke(system_prompt)
        ai_content = extract_text_content(ai_message.content)
        return {"answers": [clean_thinking_content(ai_content)]}
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


async def write_final_answer(state: ThreadState, config: RunnableConfig) -> dict:
    try:
        system_prompt = Prompter(prompt_template="ask/final_answer").render(data=state)  # type: ignore[arg-type]
        model = await provision_langchain_model(
            system_prompt,
            config.get("configurable", {}).get("final_answer_model"),
            "tools",
            max_tokens=2000,
        )
        ai_message = await model.ainvoke(system_prompt)
        final_content = extract_text_content(ai_message.content)

        # Evidence v2: build evidence metadata for the API response.
        evidence_need_val: str = state.get("evidence_need") or "overview"  # type: ignore[assignment]
        layers: list[str] = ["vector_search"]  # always present
        fallback = False

        # Check if verified table data was used (answers contain the marker)
        answers_text = str(state.get("answers", []))
        if "Verified Table Data" in answers_text:
            layers.insert(0, "verified_table_data")
        if "Full Source Evidence" in answers_text:
            layers.insert(len(layers) - 1 if layers else 0, "full_source_evidence")

        # Detect if heuristic upgrade occurred (strategy said overview but
        # evidence_need was upgraded).
        strategy = state.get("strategy")
        if strategy and hasattr(strategy, "evidence_need"):
            if strategy.evidence_need != evidence_need_val:  # type: ignore[union-attr]
                fallback = True

        evidence_meta = EvidenceMetadata(
            evidence_need=evidence_need_val,  # type: ignore[arg-type]
            evidence_layers_used=layers,
            fallback_occurred=fallback,
        )

        return {
            "final_answer": clean_thinking_content(final_content),
            "evidence_metadata": evidence_meta.model_dump(),
        }
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


agent_state = StateGraph(ThreadState)
agent_state.add_node("agent", call_model_with_messages)
agent_state.add_node("identify_table_source", identify_table_source)  # Wave 5B
agent_state.add_node("provide_answer", provide_answer)
agent_state.add_node("write_final_answer", write_final_answer)
agent_state.add_edge(START, "agent")
# Wave 5B: strategy → identify candidate table source → fan-out to searches
agent_state.add_edge("agent", "identify_table_source")
agent_state.add_conditional_edges(
    "identify_table_source", trigger_queries, ["provide_answer"]
)
agent_state.add_edge("provide_answer", "write_final_answer")
agent_state.add_edge("write_final_answer", END)

graph = agent_state.compile()
