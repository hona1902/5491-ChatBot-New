import operator
from typing import Annotated, List, Optional

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
    # Wave 5B: candidate table source for this sub-search (pass-through only,
    # no table data injection until Wave 5C). None = no table source identified.
    candidate_source_id: Optional[str]


class Search(BaseModel):
    term: str
    instructions: str = Field(
        description="Tell the answeting LLM what information you need extracted from this search"
    )


class Strategy(BaseModel):
    reasoning: str
    searches: List[Search] = Field(
        default_factory=list,
        description="You can add up to five searches to this strategy",
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
    # Not yet used for lookup injection (Wave 5C concern).
    candidate_source_id: Optional[str]


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

        return {"strategy": strategy}
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


# ---------------------------------------------------------------------------
# Wave 5B: identify_table_source
# ---------------------------------------------------------------------------
# File extensions treated as structured tabular sources eligible for lookup.
_TABULAR_EXTENSIONS: frozenset[str] = frozenset({".csv", ".xlsx"})


def _source_is_tabular(file_path: Optional[str]) -> bool:
    """Return True if file_path ends with .csv or .xlsx (case-insensitive)."""
    if not file_path:
        return False
    dot = file_path.rfind(".")
    if dot == -1:
        return False
    return file_path[dot:].lower() in _TABULAR_EXTENSIONS


async def identify_table_source(state: ThreadState, config: RunnableConfig) -> dict:
    """
    Wave 5B node: identify the single most relevant CSV/XLSX source in the
    notebook for use as a candidate table source.

    Rules (fail-closed throughout):
    1. notebook_id missing  → candidate_source_id = None.
    2. Zero CSV/XLSX sources with source_table records → None.
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
        #    Keep only CSV/XLSX sources that have at least one source_table.  #
        # ------------------------------------------------------------------ #
        nb_rid = ensure_record_id(notebook_id)

        # Simple two-step approach: fetch sources linked to notebook via
        # reference edges, then filter/count in Python.  A single complex
        # SurrealQL query would be faster but harder to test portably across
        # SurrealDB versions; simplicity wins here.
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
        # 2. Filter: only CSV/XLSX sources.                                   #
        # ------------------------------------------------------------------ #
        tabular_candidates: list[dict] = []

        for row in nb_sources_raw or []:
            src = row.get("source") or {}
            if not isinstance(src, dict):
                continue
            asset = src.get("asset") or {}
            file_path = asset.get("file_path", "") if isinstance(asset, dict) else ""
            if not _source_is_tabular(file_path):
                continue

            source_id = src.get("id") or ""
            if not source_id:
                continue

            tabular_candidates.append(
                {
                    "source_id": str(source_id),
                    "title": src.get("title") or "",
                    "topics": src.get("topics") or [],
                    "file_path": file_path,
                }
            )

        if not tabular_candidates:
            logger.debug(
                f"identify_table_source: no CSV/XLSX sources in notebook {notebook_id}."
            )
            return {"candidate_source_id": None}

        # ------------------------------------------------------------------ #
        # 3. Keep only sources that have at least one source_table record.    #
        # ------------------------------------------------------------------ #
        candidates_with_tables: list[dict] = []
        for cand in tabular_candidates:
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
                f"identify_table_source: no tabular sources with source_table "
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
    return [
        Send(
            "provide_answer",
            {
                "question": state["question"],
                "instructions": s.instructions,
                "term": s.term,
                # Wave 5B: pass candidate through to sub-graph state.
                # No injection yet — Wave 5C will use it for table lookup.
                "candidate_source_id": candidate_source_id,
            },
        )
        for s in state["strategy"].searches
    ]


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
        system_prompt = Prompter(prompt_template="ask/query_process").render(data=payload)  # type: ignore[arg-type]
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
        return {"final_answer": clean_thinking_content(final_content)}
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
