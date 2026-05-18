import operator
from typing import Any, Dict, List, Optional

from content_core import extract_content
from content_core.common import ProcessSourceState
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from loguru import logger
from typing_extensions import Annotated, TypedDict

from open_notebook.utils.docx_table_extractor import extract_docx_with_tables
from open_notebook.utils.pdf_table_preserver import extract_pdf_with_tables
from open_notebook.config import TABLES_MARKDOWN_MAX_CHARS
from open_notebook.utils.table_extractor_registry import (
    ExtractedTable,
    extract_tables_from_source,
)

from open_notebook.ai.models import Model, ModelManager
from open_notebook.domain.content_settings import ContentSettings
from open_notebook.domain.notebook import Asset, Source, SourceTable
from open_notebook.domain.transformation import Transformation
from open_notebook.graphs.transformation import graph as transform_graph


class SourceState(TypedDict):
    content_state: ProcessSourceState
    apply_transformations: List[Transformation]
    source_id: str
    notebook_ids: List[str]
    source: Source
    transformation: Annotated[list, operator.add]
    embed: bool
    extracted_tables: List[ExtractedTable]  # populated by extract_tables node
    tables_markdown: Optional[str]           # populated by extract_tables node


class TransformationState(TypedDict):
    source: Source
    transformation: Transformation


async def content_process(state: SourceState) -> dict:
    content_settings = ContentSettings(
        default_content_processing_engine_doc="auto",
        default_content_processing_engine_url="auto",
        default_embedding_option="ask",
        auto_delete_files="yes",
        youtube_preferred_languages=[
            "en",
            "pt",
            "es",
            "de",
            "nl",
            "en-GB",
            "fr",
            "hi",
            "ja",
        ],
    )
    content_state: Dict[str, Any] = state["content_state"]  # type: ignore[assignment]

    content_state["url_engine"] = (
        content_settings.default_content_processing_engine_url or "auto"
    )
    content_state["document_engine"] = (
        content_settings.default_content_processing_engine_doc or "auto"
    )
    content_state["output_format"] = "markdown"

    # Add speech-to-text model configuration from Default Models
    try:
        model_manager = ModelManager()
        defaults = await model_manager.get_defaults()
        if defaults.default_speech_to_text_model:
            stt_model = await Model.get(defaults.default_speech_to_text_model)
            if stt_model:
                content_state["audio_provider"] = stt_model.provider
                content_state["audio_model"] = stt_model.name
                logger.debug(
                    f"Using speech-to-text model: {stt_model.provider}/{stt_model.name}"
                )
    except Exception as e:
        logger.warning(f"Failed to retrieve speech-to-text model configuration: {e}")
        # Continue without custom audio model (content-core will use its default)

    processed_state = await extract_content(content_state)

    # ---- Custom table-aware post-processing ----
    # content_core's DOCX extractor skips tables entirely; its PDF extractor
    # converts tables to Markdown but then clean_pdf_text() destroys them.
    # We run our own extractors and replace processed_state.content if they succeed.
    file_path: str = processed_state.file_path or ""
    identified_type: str = processed_state.identified_type or ""

    DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PDF_MIME = "application/pdf"

    if file_path and (identified_type == DOCX_MIME or file_path.lower().endswith(".docx")):
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            custom_content = await loop.run_in_executor(
                None, extract_docx_with_tables, file_path
            )
            if custom_content and custom_content.strip():
                processed_state.content = custom_content
                logger.debug("DOCX table extractor: replaced content with table-aware output")
            else:
                logger.warning(
                    "DOCX table extractor: returned empty content — keeping content_core output"
                )
        except Exception as exc:
            logger.warning(
                f"DOCX table extractor failed, falling back to content_core output: {exc}"
            )

    elif file_path and (identified_type == PDF_MIME or file_path.lower().endswith(".pdf")):
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            custom_content = await loop.run_in_executor(
                None, extract_pdf_with_tables, file_path
            )
            if custom_content and custom_content.strip():
                processed_state.content = custom_content
                logger.debug("PDF table preserver: replaced content with table-preserved output")
            else:
                logger.warning(
                    "PDF table preserver: returned empty content — keeping content_core output"
                )
        except Exception as exc:
            logger.warning(
                f"PDF table preserver failed, falling back to content_core output: {exc}"
            )
    # ---- End custom table-aware post-processing ----

    if not processed_state.content or not processed_state.content.strip():
        url = processed_state.url or ""
        if url and ("youtube.com" in url or "youtu.be" in url):
            raise ValueError(
                "Could not extract content from this YouTube video. "
                "No transcript or subtitles are available. "
                "Try configuring a Speech-to-Text model in Settings "
                "to transcribe the audio instead."
            )
        raise ValueError(
            "Could not extract any text content from this source. "
            "The content may be empty, inaccessible, or in an unsupported format."
        )

    return {"content_state": processed_state}


async def extract_tables(state: SourceState) -> dict:
    """
    Non-blocking graph node: extract structured tables from the source file,
    persist each as a source_table record, and assemble tables_markdown.

    All failures are caught and logged — ingestion always continues.
    """
    content_state = state["content_state"]
    file_path: str = getattr(content_state, "file_path", None) or ""
    source_id: str = state["source_id"]

    if not file_path:
        logger.debug("extract_tables: no file_path in content_state; skipping table extraction")
        return {"extracted_tables": [], "tables_markdown": None}

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        tables: List[ExtractedTable] = await loop.run_in_executor(
            None, extract_tables_from_source, file_path, source_id
        )
    except Exception as exc:
        logger.warning(f"extract_tables: dispatcher raised unexpectedly for '{file_path}': {exc}")
        return {"extracted_tables": [], "tables_markdown": None}

    if not tables:
        logger.debug(f"extract_tables: no tables extracted from '{file_path}'")
        return {"extracted_tables": [], "tables_markdown": None}

    logger.info(f"extract_tables: persisting {len(tables)} table(s) for source '{source_id}'")

    # Persist source_table records
    from open_notebook.database.repository import ensure_record_id, repo_query

    # Clear any existing source_table records for idempotency
    try:
        await repo_query(
            "DELETE source_table WHERE source = $source_id",
            {"source_id": ensure_record_id(source_id)},
        )
    except Exception as exc:
        logger.warning(f"extract_tables: failed to clear old source_table records: {exc}")

    markdown_parts: List[str] = []
    for table in tables:
        try:
            st = SourceTable(
                source=str(ensure_record_id(source_id)),
                table_id=table.table_id,
                page_number=table.page_number,
                sheet_name=table.sheet_name,
                column_headers=table.column_headers,
                # Phase 2: persist original (pre-dedup) headers when present
                original_headers=table.original_headers,
                row_data=table.row_data,
                markdown_repr=table.markdown_repr,
                row_count=table.row_count,
                col_count=table.col_count,
                truncated=table.truncated,
            )
            await st.save()
            logger.debug(f"extract_tables: saved source_table record for table_id='{table.table_id}'")

            # Build markdown header comment for this table
            label_parts = [f"Table: {table.table_id}"]
            if table.page_number is not None:
                label_parts.append(f"Page {table.page_number}")
            if table.sheet_name:
                label_parts.append(f"Sheet: {table.sheet_name}")
            if table.truncated:
                label_parts.append("[truncated]")
            markdown_parts.append(
                f"<!-- {', '.join(label_parts)} -->\n{table.markdown_repr}"
            )
        except Exception as exc:
            logger.warning(
                f"extract_tables: failed to save SourceTable for table_id='{table.table_id}': {exc}"
            )

    # Phase 2: assemble tables_markdown with a hard character cap.
    # We stop at the last complete table boundary before TABLES_MARKDOWN_MAX_CHARS
    # and append a truncation marker. This keeps source.tables_markdown bounded
    # regardless of how many tables were extracted.
    if not markdown_parts:
        tables_markdown = None
    else:
        assembled: List[str] = []
        running_chars = 0
        separator = "\n\n"
        sep_len = len(separator)
        omitted = 0

        for part in markdown_parts:
            # Account for the separator that joins parts
            candidate_addition = (sep_len if assembled else 0) + len(part)
            if running_chars + candidate_addition > TABLES_MARKDOWN_MAX_CHARS:
                omitted = len(markdown_parts) - len(assembled)
                logger.warning(
                    f"extract_tables: tables_markdown cap ({TABLES_MARKDOWN_MAX_CHARS} chars) "
                    f"reached for source '{source_id}'; {omitted} table(s) omitted"
                )
                break
            assembled.append(part)
            running_chars += candidate_addition

        tables_markdown = separator.join(assembled)
        if omitted > 0:
            tables_markdown += (
                f"\n\n<!-- tables_markdown truncated: {omitted} table(s) omitted "
                f"(OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS={TABLES_MARKDOWN_MAX_CHARS}) -->"
            )
        # Edge case: zero tables fit (first table alone exceeds the cap)
        if not assembled:
            tables_markdown = None

    return {"extracted_tables": tables, "tables_markdown": tables_markdown}


async def save_source(state: SourceState) -> dict:
    content_state = state["content_state"]

    # Get existing source using the provided source_id
    source = await Source.get(state["source_id"])
    if not source:
        raise ValueError(f"Source with ID {state['source_id']} not found")

    # Update the source with processed content
    source.asset = Asset(url=content_state.url, file_path=content_state.file_path)
    source.full_text = content_state.content

    # Write tables_markdown from extract_tables node (independent of full_text truncation)
    tables_markdown = state.get("tables_markdown")
    if tables_markdown:
        source.tables_markdown = tables_markdown
        logger.debug(
            f"save_source: writing tables_markdown ({len(tables_markdown)} chars) for source {source.id}"
        )

    # Preserve user-set title; only overwrite placeholder or empty titles
    if content_state.title and (not source.title or source.title == "Processing..."):
        source.title = content_state.title

    await source.save()

    # NOTE: Notebook associations are created by the API immediately for UI responsiveness
    # No need to create them here to avoid duplicate edges

    if state["embed"]:
        if source.full_text and source.full_text.strip():
            logger.debug("Embedding content for vector search")
            await source.vectorize()
        else:
            logger.warning(
                f"Source {source.id} has no text content to embed, skipping vectorization"
            )

    return {"source": source}


def trigger_transformations(state: SourceState, config: RunnableConfig) -> List[Send]:
    if len(state["apply_transformations"]) == 0:
        return []

    to_apply = state["apply_transformations"]
    logger.debug(f"Applying transformations {to_apply}")

    return [
        Send(
            "transform_content",
            {
                "source": state["source"],
                "transformation": t,
            },
        )
        for t in to_apply
    ]


async def transform_content(state: TransformationState) -> Optional[dict]:
    source = state["source"]
    content = source.full_text
    if not content:
        return None
    transformation: Transformation = state["transformation"]

    logger.debug(f"Applying transformation {transformation.name}")
    result = await transform_graph.ainvoke(
        dict(input_text=content, transformation=transformation)  # type: ignore[arg-type]
    )
    await source.add_insight(transformation.title, result["output"])
    return {
        "transformation": [
            {
                "output": result["output"],
                "transformation_name": transformation.name,
            }
        ]
    }


# Create and compile the workflow
workflow = StateGraph(SourceState)

# Add nodes
workflow.add_node("content_process", content_process)
workflow.add_node("extract_tables", extract_tables)
workflow.add_node("save_source", save_source)
workflow.add_node("transform_content", transform_content)
# Define the graph edges
workflow.add_edge(START, "content_process")
workflow.add_edge("content_process", "extract_tables")
workflow.add_edge("extract_tables", "save_source")
workflow.add_conditional_edges(
    "save_source", trigger_transformations, ["transform_content"]
)
workflow.add_edge("transform_content", END)

# Compile the graph
source_graph = workflow.compile()
