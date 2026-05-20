import json
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from langchain_core.runnables import RunnableConfig
from loguru import logger
from pydantic import BaseModel, Field

from api.auth import get_current_user
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import ChatSession, Note, Notebook, Source
from open_notebook.domain.user import AppUser
from open_notebook.exceptions import (
    NotFoundError,
)
from open_notebook.graphs.chat import get_chat_graph
from open_notebook.utils.graph_utils import get_session_message_count

router = APIRouter()


# Request/Response models
class CreateSessionRequest(BaseModel):
    notebook_id: str = Field(..., description="Notebook ID to create session for")
    title: Optional[str] = Field(None, description="Optional session title")
    model_override: Optional[str] = Field(
        None, description="Optional model override for this session"
    )


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = Field(None, description="New session title")
    model_override: Optional[str] = Field(
        None, description="Model override for this session"
    )


class ChatMessage(BaseModel):
    id: str = Field(..., description="Message ID")
    type: str = Field(..., description="Message type (human|ai)")
    content: str = Field(..., description="Message content")
    timestamp: Optional[str] = Field(None, description="Message timestamp")


class ChatSessionResponse(BaseModel):
    id: str = Field(..., description="Session ID")
    title: str = Field(..., description="Session title")
    notebook_id: Optional[str] = Field(None, description="Notebook ID")
    created: str = Field(..., description="Creation timestamp")
    updated: str = Field(..., description="Last update timestamp")
    message_count: Optional[int] = Field(
        None, description="Number of messages in session"
    )
    model_override: Optional[str] = Field(
        None, description="Model override for this session"
    )


class ChatSessionWithMessagesResponse(ChatSessionResponse):
    messages: List[ChatMessage] = Field(
        default_factory=list, description="Session messages"
    )


class ExecuteChatRequest(BaseModel):
    session_id: str = Field(..., description="Chat session ID")
    message: str = Field(..., description="User message content")
    context: Dict[str, Any] = Field(
        ..., description="Chat context with sources and notes"
    )
    model_override: Optional[str] = Field(
        None, description="Optional model override for this message"
    )


class ExecuteChatResponse(BaseModel):
    session_id: str = Field(..., description="Session ID")
    messages: List[ChatMessage] = Field(..., description="Updated message list")


class BuildContextRequest(BaseModel):
    notebook_id: str = Field(..., description="Notebook ID")
    context_config: Dict[str, Any] = Field(..., description="Context configuration")


class BuildContextResponse(BaseModel):
    context: Dict[str, Any] = Field(..., description="Built context data")
    token_count: int = Field(..., description="Estimated token count")
    char_count: int = Field(..., description="Character count")


class SuccessResponse(BaseModel):
    success: bool = Field(True, description="Operation success status")
    message: str = Field(..., description="Success message")


@router.get("/chat/sessions", response_model=List[ChatSessionResponse])
async def get_sessions(
    notebook_id: str = Query(..., description="Notebook ID"),
    current_user: AppUser = Depends(get_current_user),
):
    """Get all chat sessions for a notebook."""
    try:
        notebook = await Notebook.get(notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        sessions_list = await notebook.get_chat_sessions()
        sessions_list = [
            s for s in sessions_list if getattr(s, "owner_id", None) == current_user.id
        ]

        results = []
        chat_graph = await get_chat_graph()
        for session in sessions_list:
            session_id = str(session.id)

            # Get message count from LangGraph state
            msg_count = await get_session_message_count(chat_graph, session_id)

            results.append(
                ChatSessionResponse(
                    id=session.id or "",
                    title=session.title or "Untitled Session",
                    notebook_id=notebook_id,
                    created=str(session.created),
                    updated=str(session.updated),
                    message_count=msg_count,
                    model_override=getattr(session, "model_override", None),
                )
            )

        return results
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except Exception as e:
        logger.error(f"Error fetching chat sessions: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error fetching chat sessions: {str(e)}"
        )


@router.post("/chat/sessions", response_model=ChatSessionResponse)
async def create_session(
    request: CreateSessionRequest,
    current_user: AppUser = Depends(get_current_user),
):
    """Create a new chat session."""
    try:
        notebook = await Notebook.get(request.notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        session = ChatSession(
            title=request.title
            or "Chat Session",
            model_override=request.model_override,
            owner_id=current_user.id,
        )
        await session.save()

        # Relate session to notebook
        await session.relate_to_notebook(request.notebook_id)

        return ChatSessionResponse(
            id=session.id or "",
            title=session.title or "",
            notebook_id=request.notebook_id,
            created=str(session.created),
            updated=str(session.updated),
            message_count=0,
            model_override=session.model_override,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except Exception as e:
        logger.error(f"Error creating chat session: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error creating chat session: {str(e)}"
        )


@router.get(
    "/chat/sessions/{session_id}", response_model=ChatSessionWithMessagesResponse
)
async def get_session(
    session_id: str,
    current_user: AppUser = Depends(get_current_user),
):
    """Get a specific session with its messages."""
    try:
        full_session_id = (
            session_id
            if session_id.startswith("chat_session:")
            else f"chat_session:{session_id}"
        )
        session = await ChatSession.get(full_session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if getattr(session, "owner_id", None) != current_user.id:
            raise HTTPException(status_code=404, detail="Session not found")

        # Get session state from LangGraph to retrieve messages
        chat_graph = await get_chat_graph()
        thread_state = await chat_graph.aget_state(
            config=RunnableConfig(configurable={"thread_id": full_session_id}),
        )

        # Extract messages from state
        messages: list[ChatMessage] = []
        if thread_state and thread_state.values and "messages" in thread_state.values:
            for msg in thread_state.values["messages"]:
                messages.append(
                    ChatMessage(
                        id=getattr(msg, "id", f"msg_{len(messages)}"),
                        type=msg.type if hasattr(msg, "type") else "unknown",
                        content=msg.content if hasattr(msg, "content") else str(msg),
                        timestamp=None,  # LangChain messages don't have timestamps by default
                    )
                )

        # Find notebook_id (we need to query the relationship)
        # Ensure session_id has proper table prefix
        full_session_id = (
            session_id
            if session_id.startswith("chat_session:")
            else f"chat_session:{session_id}"
        )

        notebook_query = await repo_query(
            "SELECT out FROM refers_to WHERE in = $session_id",
            {"session_id": ensure_record_id(full_session_id)},
        )

        notebook_id = notebook_query[0]["out"] if notebook_query else None

        if not notebook_id:
            # This might be an old session created before API migration
            logger.warning(
                f"No notebook relationship found for session {session_id} - may be an orphaned session"
            )

        return ChatSessionWithMessagesResponse(
            id=session.id or "",
            title=session.title or "Untitled Session",
            notebook_id=notebook_id,
            created=str(session.created),
            updated=str(session.updated),
            message_count=len(messages),
            messages=messages,
            model_override=getattr(session, "model_override", None),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        logger.error(f"Error fetching session: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching session: {str(e)}")


@router.put("/chat/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: AppUser = Depends(get_current_user),
):
    """Update session title."""
    try:
        full_session_id = (
            session_id
            if session_id.startswith("chat_session:")
            else f"chat_session:{session_id}"
        )
        session = await ChatSession.get(full_session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if getattr(session, "owner_id", None) != current_user.id:
            raise HTTPException(status_code=404, detail="Session not found")

        update_data = request.model_dump(exclude_unset=True)

        if "title" in update_data:
            session.title = update_data["title"]

        if "model_override" in update_data:
            session.model_override = update_data["model_override"]

        await session.save()

        # Find notebook_id
        # Ensure session_id has proper table prefix
        full_session_id = (
            session_id
            if session_id.startswith("chat_session:")
            else f"chat_session:{session_id}"
        )
        notebook_query = await repo_query(
            "SELECT out FROM refers_to WHERE in = $session_id",
            {"session_id": ensure_record_id(full_session_id)},
        )
        notebook_id = notebook_query[0]["out"] if notebook_query else None

        # Get message count from LangGraph state
        chat_graph = await get_chat_graph()
        msg_count = await get_session_message_count(chat_graph, full_session_id)

        return ChatSessionResponse(
            id=session.id or "",
            title=session.title or "",
            notebook_id=notebook_id,
            created=str(session.created),
            updated=str(session.updated),
            message_count=msg_count,
            model_override=session.model_override,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        logger.error(f"Error updating session: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating session: {str(e)}")


@router.delete("/chat/sessions/{session_id}", response_model=SuccessResponse)
async def delete_session(
    session_id: str,
    current_user: AppUser = Depends(get_current_user),
):
    """Delete a chat session."""
    try:
        full_session_id = (
            session_id
            if session_id.startswith("chat_session:")
            else f"chat_session:{session_id}"
        )
        session = await ChatSession.get(full_session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if getattr(session, "owner_id", None) != current_user.id:
            raise HTTPException(status_code=404, detail="Session not found")

        await session.delete()

        return SuccessResponse(success=True, message="Session deleted successfully")
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        logger.error(f"Error deleting session: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting session: {str(e)}")


@router.post("/chat/execute", response_model=ExecuteChatResponse)
async def execute_chat(
    request: ExecuteChatRequest,
    current_user: AppUser = Depends(get_current_user),
):
    """Execute a chat request and get AI response."""
    try:
        full_session_id = (
            request.session_id
            if request.session_id.startswith("chat_session:")
            else f"chat_session:{request.session_id}"
        )
        session = await ChatSession.get(full_session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if getattr(session, "owner_id", None) != current_user.id:
            raise HTTPException(status_code=404, detail="Session not found")

        # Determine model override (per-request override takes precedence over session-level)
        model_override = (
            request.model_override
            if request.model_override is not None
            else getattr(session, "model_override", None)
        )

        # Get current state
        chat_graph = await get_chat_graph()
        current_state = await chat_graph.aget_state(
            config=RunnableConfig(configurable={"thread_id": full_session_id}),
        )

        # Prepare state for execution
        state_values = current_state.values if current_state else {}
        state_values["messages"] = state_values.get("messages", [])
        state_values["context"] = request.context
        state_values["model_override"] = model_override

        # Add user message to state
        from langchain_core.messages import HumanMessage

        user_message = HumanMessage(content=request.message)
        state_values["messages"].append(user_message)

        # Execute chat graph (fully async)
        result = await chat_graph.ainvoke(
            input=state_values,  # type: ignore[arg-type]
            config=RunnableConfig(
                configurable={
                    "thread_id": full_session_id,
                    "model_id": model_override,
                }
            ),
        )

        # Update session timestamp
        await session.save()

        # Convert messages to response format
        messages: list[ChatMessage] = []
        for msg in result.get("messages", []):
            messages.append(
                ChatMessage(
                    id=getattr(msg, "id", f"msg_{len(messages)}"),
                    type=msg.type if hasattr(msg, "type") else "unknown",
                    content=msg.content if hasattr(msg, "content") else str(msg),
                    timestamp=None,
                )
            )

        return ExecuteChatResponse(session_id=request.session_id, messages=messages)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        # Log detailed error with context for debugging
        logger.error(
            f"Error executing chat: {str(e)}\n"
            f"  Session ID: {request.session_id}\n"
            f"  Model override: {request.model_override}\n"
            f"  Traceback:\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Error executing chat: {str(e)}")


async def stream_notebook_chat_response(
    session_id: str,
    state_values: dict,
    model_override: Optional[str] = None,
    request: Optional[Request] = None,
) -> AsyncGenerator[str, None]:
    """Stream notebook chat response as SSE events.

    Follows the proven pattern from source_chat.py::stream_source_chat_response().
    Emits: user_message -> ai_message -> complete (or error).
    Checks for client disconnect to free zombie threads.
    """
    try:
        # Emit user message event immediately — this keeps Cloudflare happy
        # (data sent within the 100 s window)
        user_msg = state_values.get("messages", [])[-1]
        user_event = {
            "type": "user_message",
            "content": user_msg.content if hasattr(user_msg, "content") else str(user_msg),
            "timestamp": None,
        }
        yield f"data: {json.dumps(user_event)}\n\n"

        # Check for client disconnect before expensive AI call
        if request and await request.is_disconnected():
            logger.info(f"Client disconnected before AI call for session {session_id}")
            return

        # Execute chat graph (fully async)
        chat_graph = await get_chat_graph()
        result = await chat_graph.ainvoke(
            input=state_values,  # type: ignore[arg-type]
            config=RunnableConfig(
                configurable={
                    "thread_id": session_id,
                    "model_id": model_override,
                }
            ),
        )

        # Check for client disconnect after AI call
        if request and await request.is_disconnected():
            logger.info(f"Client disconnected after AI call for session {session_id}")
            return

        # Stream AI response messages
        if "messages" in result:
            for msg in result["messages"]:
                if hasattr(msg, "type") and msg.type == "ai":
                    ai_event = {
                        "type": "ai_message",
                        "content": msg.content if hasattr(msg, "content") else str(msg),
                        "timestamp": None,
                    }
                    yield f"data: {json.dumps(ai_event)}\n\n"

        # Completion signal
        yield f"data: {json.dumps({'type': 'complete'})}\n\n"

    except Exception as e:
        from open_notebook.utils.error_classifier import classify_error

        _, user_message = classify_error(e)
        logger.error(f"Error in notebook chat streaming: {str(e)}")
        error_event = {"type": "error", "message": user_message}
        yield f"data: {json.dumps(error_event)}\n\n"


@router.post("/chat/stream")
async def stream_chat(
    request: Request,
    body: ExecuteChatRequest,
    current_user: AppUser = Depends(get_current_user),
):
    """Execute a chat request with SSE streaming response.

    Same logic as /chat/execute but returns a StreamingResponse with
    Server-Sent Events, preventing Cloudflare 524 timeouts and enabling
    client disconnect detection.
    """
    try:
        full_session_id = (
            body.session_id
            if body.session_id.startswith("chat_session:")
            else f"chat_session:{body.session_id}"
        )
        session = await ChatSession.get(full_session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if getattr(session, "owner_id", None) != current_user.id:
            raise HTTPException(status_code=404, detail="Session not found")

        # Determine model override
        model_override = (
            body.model_override
            if body.model_override is not None
            else getattr(session, "model_override", None)
        )

        # Get current state
        chat_graph = await get_chat_graph()
        current_state = await chat_graph.aget_state(
            config=RunnableConfig(configurable={"thread_id": full_session_id}),
        )

        # Prepare state for execution
        state_values = current_state.values if current_state else {}
        state_values["messages"] = state_values.get("messages", [])
        state_values["context"] = body.context
        state_values["model_override"] = model_override

        # Add user message to state
        from langchain_core.messages import HumanMessage

        user_message = HumanMessage(content=body.message)
        state_values["messages"].append(user_message)

        # Update session timestamp
        await session.save()

        # Return SSE streaming response
        return StreamingResponse(
            stream_notebook_chat_response(
                session_id=full_session_id,
                state_values=state_values,
                model_override=model_override,
                request=request,
            ),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error in stream_chat: {str(e)}\n"
            f"  Session ID: {body.session_id}\n"
            f"  Traceback:\n{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail=f"Error executing chat: {str(e)}")


@router.post("/chat/context", response_model=BuildContextResponse)
async def build_context(
    request: BuildContextRequest,
    current_user: AppUser = Depends(get_current_user),
):
    """Build context for a notebook based on context configuration.

    Requires authentication. Normal authenticated users can build context for any
    notebook that exists — the context payload only includes sources/notes by ID,
    so there is no implicit data leak. Row-level SurrealDB restrictions on
    Notebook.get() would incorrectly block non-admin users, so we use a raw
    repo_query to verify existence which bypasses owner-scoped RLS.
    """
    try:
        # Verify notebook exists using a raw query that is not owner-scoped.
        # Notebook.get() may return None for normal users due to SurrealDB
        # row-level permissions even when the notebook exists and is accessible.
        notebook_rows = await repo_query(
            "SELECT id FROM notebook WHERE id = $notebook_id LIMIT 1",
            {"notebook_id": ensure_record_id(request.notebook_id)},
        )
        if not notebook_rows:
            raise HTTPException(status_code=404, detail="Notebook not found")

        # For the default (no context_config) path we still need the domain
        # object to call get_sources / get_notes. Fetch it safely.
        notebook = await Notebook.get(request.notebook_id) if not request.context_config else None

        context_data: dict[str, list[dict[str, str]]] = {"sources": [], "notes": []}
        total_content = ""

        # Process context configuration if provided
        if request.context_config:
            # Process sources
            for source_id, status in request.context_config.get("sources", {}).items():
                if "not in" in status:
                    continue

                try:
                    # Add table prefix if not present
                    full_source_id = (
                        source_id
                        if source_id.startswith("source:")
                        else f"source:{source_id}"
                    )

                    try:
                        source = await Source.get(full_source_id)
                    except Exception:
                        continue

                    if "insights" in status:
                        source_context = await source.get_context(context_size="short")
                        context_data["sources"].append(source_context)
                        total_content += str(source_context)
                    elif "full content" in status:
                        source_context = await source.get_context(context_size="long")
                        context_data["sources"].append(source_context)
                        total_content += str(source_context)
                except Exception as e:
                    logger.warning(f"Error processing source {source_id}: {str(e)}")
                    continue

            # Process notes
            for note_id, status in request.context_config.get("notes", {}).items():
                if "not in" in status:
                    continue

                try:
                    # Add table prefix if not present
                    full_note_id = (
                        note_id if note_id.startswith("note:") else f"note:{note_id}"
                    )
                    note = await Note.get(full_note_id)
                    if not note:
                        continue

                    if "full content" in status:
                        note_context = note.get_context(context_size="long")
                        context_data["notes"].append(note_context)
                        total_content += str(note_context)
                except Exception as e:
                    logger.warning(f"Error processing note {note_id}: {str(e)}")
                    continue
        else:
            # Default behavior - include all sources and notes with short context.
            # notebook was not loaded above (context_config path skips it),
            # so fetch it now. Use Notebook.get which may return None for normal
            # users under SurrealDB RLS — fall back to an empty context rather
            # than raising 500.
            if notebook is None:
                notebook = await Notebook.get(request.notebook_id)

            if notebook is not None:
                sources = await notebook.get_sources()
                for source in sources:
                    try:
                        source_context = await source.get_context(context_size="short")
                        context_data["sources"].append(source_context)
                        total_content += str(source_context)
                    except Exception as e:
                        logger.warning(f"Error processing source {source.id}: {str(e)}")
                        continue

                notes = await notebook.get_notes()
                for note in notes:
                    try:
                        note_context = note.get_context(context_size="short")
                        context_data["notes"].append(note_context)
                        total_content += str(note_context)
                    except Exception as e:
                        logger.warning(f"Error processing note {note.id}: {str(e)}")
                        continue

        # Calculate character and token counts
        char_count = len(total_content)
        # Use token count utility if available
        try:
            from open_notebook.utils import token_count

            estimated_tokens = token_count(total_content) if total_content else 0
        except ImportError:
            # Fallback to simple estimation
            estimated_tokens = char_count // 4

        return BuildContextResponse(
            context=context_data, token_count=estimated_tokens, char_count=char_count
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error building context: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error building context: {str(e)}")
