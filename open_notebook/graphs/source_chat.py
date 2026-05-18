from typing import Annotated, Dict, List, Optional

from ai_prompter import Prompter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from open_notebook.ai.provision import provision_langchain_model
from open_notebook.domain.notebook import Source, SourceInsight
from open_notebook.exceptions import OpenNotebookError
from open_notebook.graphs.checkpoint import get_memory
from open_notebook.utils import clean_thinking_content
from open_notebook.utils.context_builder import ContextBuilder
from open_notebook.utils.error_classifier import classify_error
from open_notebook.utils.table_lookup import table_exact_lookup
from open_notebook.utils.text_utils import extract_text_content


class SourceChatState(TypedDict):
    messages: Annotated[list, add_messages]
    source_id: str
    source: Optional[Source]
    insights: Optional[List[SourceInsight]]
    context: Optional[str]
    model_override: Optional[str]
    context_indicators: Optional[Dict[str, List[str]]]


async def acall_model_with_source_context(
    state: SourceChatState, config: RunnableConfig
) -> dict:
    """Async graph node — builds source context and calls the model.

    Runs entirely on FastAPI's main event loop.  No secondary event loops
    or ThreadPoolExecutor hacks are needed.
    """
    try:
        return await _acall_model_with_source_context_inner(state, config)
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


async def _acall_model_with_source_context_inner(
    state: SourceChatState, config: RunnableConfig
) -> dict:
    source_id = state.get("source_id")
    if not source_id:
        raise ValueError("source_id is required in state")

    # Build source context — ContextBuilder.build() is already async
    context_builder = ContextBuilder(
        source_id=source_id,
        include_insights=True,
        include_notes=False,  # Focus on source-specific content
        max_tokens=50000,  # Reasonable limit for source context
    )
    context_data = await context_builder.build()

    # Extract source and insights from context
    source = None
    insights = []
    context_indicators: dict[str, list[str | None]] = {
        "sources": [],
        "insights": [],
        "notes": [],
    }

    if context_data.get("sources"):
        source_info = context_data["sources"][0]  # First source
        source = Source(**source_info) if isinstance(source_info, dict) else source_info
        context_indicators["sources"].append(source.id)

    if context_data.get("insights"):
        for insight_data in context_data["insights"]:
            insight = (
                SourceInsight(**insight_data)
                if isinstance(insight_data, dict)
                else insight_data
            )
            insights.append(insight)
            context_indicators["insights"].append(insight.id)

    # Format context for the prompt
    formatted_context = _format_source_context(context_data)

    # --- Phase 1C: Deterministic exact lookup (CSV/XLSX sources only) -------
    # Run before the LLM call so verified cell-level data can anchor the answer.
    # Returns None for non-structured sources, missing tables, or no cell match.
    # Never raises — any error falls through silently to vector-based context.
    verified_table_section: Optional[str] = None
    if source_id:
        last_user_message = ""
        for msg in reversed(state.get("messages", [])):
            # Use only genuine HumanMessage so SystemMessage / AIMessage content
            # is never passed as the exact-lookup query.
            if isinstance(msg, HumanMessage):
                content = msg.content
                last_user_message = content if isinstance(content, str) else str(content)
                break

        if last_user_message:
            verified_table_section = await table_exact_lookup(
                query=last_user_message, source_id=source_id
            )

    # Prepend Verified Table Data section to the formatted context so it
    # appears before the ## SOURCE CONTENT block; the LLM sees it first and
    # can use it as its primary reference for exact-value questions.
    if verified_table_section:
        formatted_context = (
            "## Verified Table Data\n"
            "The following rows were retrieved by deterministic exact lookup "
            "from the structured source. Prefer these over prose for exact-value "
            "questions. Cite the table/page/sheet when answering.\n\n"
            + verified_table_section
            + "\n\n"
            + formatted_context
        )
    # -------------------------------------------------------------------------

    # Build prompt data for the template
    prompt_data = {
        "source": source.model_dump() if source else None,
        "insights": [insight.model_dump() for insight in insights] if insights else [],
        "context": formatted_context,
        "context_indicators": context_indicators,
    }

    # Apply the source_chat prompt template
    system_prompt = Prompter(prompt_template="source_chat/system").render(
        data=prompt_data
    )
    payload = [SystemMessage(content=system_prompt)] + state.get("messages", [])

    # Provision model — already async, just await directly
    model_id = (
        config.get("configurable", {}).get("model_id") or state.get("model_override")
    )
    model = await provision_langchain_model(
        str(payload), model_id, "chat", max_tokens=8192
    )

    # Use async invoke so the HTTP call to the AI provider doesn't block
    ai_message = await model.ainvoke(payload)

    # Clean thinking content from AI response (e.g., <think>...</think> tags)
    content = extract_text_content(ai_message.content)
    cleaned_content = clean_thinking_content(content)
    cleaned_message = ai_message.model_copy(update={"content": cleaned_content})

    # Update state with context information
    return {
        "messages": cleaned_message,
        "source": source,
        "insights": insights,
        "context": formatted_context,
        "context_indicators": context_indicators,
    }


def _format_source_context(context_data: Dict) -> str:
    """
    Format the context data into a readable string for the prompt.

    Args:
        context_data: Context data from ContextBuilder

    Returns:
        Formatted context string
    """
    context_parts = []

    # Add source information
    if context_data.get("sources"):
        context_parts.append("## SOURCE CONTENT")
        for source in context_data["sources"]:
            if isinstance(source, dict):
                context_parts.append(f"**Source ID:** {source.get('id', 'Unknown')}")
                context_parts.append(f"**Title:** {source.get('title', 'No title')}")
                if source.get("full_text"):
                    # Truncate full text if too long
                    full_text = source["full_text"]
                    if len(full_text) > 5000:
                        full_text = full_text[:5000] + "...\n[Content truncated]"
                    context_parts.append(f"**Content:**\n{full_text}")
                # Append tables_markdown AFTER the prose section so the
                # 5,000-char truncation above never silently drops table data.
                tables_markdown = source.get("tables_markdown")
                if tables_markdown:
                    context_parts.append("\n## TABLE DATA")
                    context_parts.append(
                        "The following structured tables were extracted from this source. "
                        "Prefer these for exact-value questions.\n"
                    )
                    context_parts.append(tables_markdown)
                context_parts.append("")  # Empty line for separation

    # Add insights
    if context_data.get("insights"):
        context_parts.append("## SOURCE INSIGHTS")
        for insight in context_data["insights"]:
            if isinstance(insight, dict):
                context_parts.append(f"**Insight ID:** {insight.get('id', 'Unknown')}")
                context_parts.append(
                    f"**Type:** {insight.get('insight_type', 'Unknown')}"
                )
                context_parts.append(
                    f"**Content:** {insight.get('content', 'No content')}"
                )
                context_parts.append("")  # Empty line for separation

    # Add metadata
    if context_data.get("metadata"):
        metadata = context_data["metadata"]
        context_parts.append("## CONTEXT METADATA")
        context_parts.append(f"- Source count: {metadata.get('source_count', 0)}")
        context_parts.append(f"- Insight count: {metadata.get('insight_count', 0)}")
        context_parts.append(f"- Total tokens: {context_data.get('total_tokens', 0)}")
        context_parts.append("")

    return "\n".join(context_parts)


async def _build_source_chat_graph():
    """Build and return the compiled source chat graph."""
    memory = await get_memory()
    source_chat_state = StateGraph(SourceChatState)
    source_chat_state.add_node("source_chat_agent", acall_model_with_source_context)
    source_chat_state.add_edge(START, "source_chat_agent")
    source_chat_state.add_edge("source_chat_agent", END)
    return source_chat_state.compile(checkpointer=memory)


# Lazy singleton
_source_chat_graph = None


async def get_source_chat_graph():
    """Return the compiled source chat graph (lazy async init)."""
    global _source_chat_graph
    if _source_chat_graph is None:
        _source_chat_graph = await _build_source_chat_graph()
    return _source_chat_graph
