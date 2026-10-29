import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import DB, Owner, owned_session
from app.core.errors import NotFoundError
from app.core.rate_limit import chat_limit
from app.models import ChatMessage, ChatSession, Document
from app.schemas.chat import (
    AskRequest,
    AskResponse,
    MessageOut,
    MessagePage,
    SessionCreate,
    SessionDetail,
    SessionOut,
)
from app.services.chat import ask, ask_stream

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED, summary="Start a chat session")
async def create_session(body: SessionCreate, db: DB, owner: Owner):
    ids = list(dict.fromkeys(body.document_ids))
    if ids:
        found = set(
            await db.scalars(select(Document.id).where(Document.owner_id == owner, Document.id.in_(ids)))
        )
        if missing := [str(i) for i in ids if i not in found]:
            raise NotFoundError("Some documents were not found", details={"missing": missing})
    session = ChatSession(owner_id=owner, title=body.title, document_ids=ids, message_count=0)
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


@router.get("/sessions", response_model=list[SessionOut], summary="List chat sessions (most recent first)")
async def list_sessions(
    db: DB, owner: Owner, limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Annotated[int, Query(ge=0)] = 0
):
    rows = await db.scalars(
        select(ChatSession)
        .where(ChatSession.owner_id == owner)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows)


@router.get("/sessions/{session_id}", response_model=SessionDetail, summary="Session with its latest messages")
async def get_session(session_id: uuid.UUID, db: DB, owner: Owner):
    session = await owned_session(db, owner, session_id)
    msgs = await db.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at.desc()).limit(50)
    )
    return SessionDetail(
        **SessionOut.model_validate(session).model_dump(),
        messages=[MessageOut.model_validate(m) for m in reversed(list(msgs))],
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a chat session")
async def delete_session(session_id: uuid.UUID, db: DB, owner: Owner):
    await db.delete(await owned_session(db, owner, session_id))


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessagePage,
    summary="Chat history, newest page first; paginate with `before`",
)
async def get_messages(
    session_id: uuid.UUID,
    db: DB,
    owner: Owner,
    before: Annotated[datetime | None, Query(description="Cursor: created_at of the oldest message you have")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    await owned_session(db, owner, session_id)
    stmt = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if before:
        stmt = stmt.where(ChatMessage.created_at < before)
    rows = list(await db.scalars(stmt.order_by(ChatMessage.created_at.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    return MessagePage(items=[MessageOut.model_validate(m) for m in reversed(rows[:limit])], has_more=has_more)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=AskResponse,
    dependencies=[chat_limit],
    summary="Ask a question (JSON, or SSE stream when stream=true)",
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE stream when stream=true"}},
)
async def ask_question(session_id: uuid.UUID, body: AskRequest, db: DB, owner: Owner):
    session = await owned_session(db, owner, session_id)
    if body.stream:
        return StreamingResponse(
            ask_stream(session.id, owner, body.question.strip()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await ask(db, session, body.question.strip())


@router.get("/sessions/{session_id}/stats", summary="Token usage for a session", include_in_schema=True)
async def session_stats(session_id: uuid.UUID, db: DB, owner: Owner):
    await owned_session(db, owner, session_id)
    row = (
        await db.execute(
            select(
                func.count(ChatMessage.id),
                func.coalesce(func.sum(ChatMessage.prompt_tokens), 0),
                func.coalesce(func.sum(ChatMessage.completion_tokens), 0),
                func.avg(ChatMessage.latency_ms),
            ).where(ChatMessage.session_id == session_id, ChatMessage.role == "assistant")
        )
    ).one()
    return {"answers": row[0], "prompt_tokens": row[1], "completion_tokens": row[2], "avg_latency_ms": row[3]}
