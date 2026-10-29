"""RAG chat.

Per turn:
  1. load recent history
  2. rewrite follow-up into a standalone query (only when there is history)
  3. hybrid retrieval scoped to the session's documents
  4. pack sources into the token budget, numbered S1..Sn
  5. grounded generation (sync JSON or SSE stream)
  6. parse [S#] markers -> citations actually used
  7. follow-up suggestions (cheap model, best effort)
  8. persist both messages + usage in one transaction
"""

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import UpstreamAIError
from app.db.session import AsyncSessionLocal
from app.models import ChatMessage, ChatSession, Document, DocumentStatus, MessageRole
from app.services import prompts
from app.services.llm import AIProviderError, Completion, Message, StreamResult, get_provider, usage_row
from app.services.retrieval import RetrievedChunk, hybrid_search

logger = logging.getLogger(__name__)

_CITATION = re.compile(r"\[(S\d+(?:\s*[,;]\s*S\d+)*)\]")
NOT_FOUND = "I couldn't find anything relevant to that in your documents."
NOT_READY = "None of the documents in this chat are ready yet. Please try again once processing has finished."


@dataclass
class PreparedTurn:
    question: str
    standalone: str
    messages: list[Message]
    sources: dict[str, RetrievedChunk]
    retrieval_ms: int
    canned_answer: str | None = None
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})


def strip_citations(text: str) -> str:
    return _CITATION.sub("", text)


def extract_citations(answer: str, sources: dict[str, RetrievedChunk]) -> list[dict]:
    used: list[str] = []
    for group in _CITATION.findall(answer):
        for sid in re.split(r"\s*[,;]\s*", group):
            if sid in sources and sid not in used:
                used.append(sid)
    return [
        {
            "source_id": sid,
            "document_id": str(sources[sid].document_id),
            "filename": sources[sid].filename,
            "chunk_index": sources[sid].chunk_index,
            "pages": sources[sid].pages,
            "snippet": sources[sid].content[:400],
            "score": sources[sid].score,
        }
        for sid in used
    ]


async def _history(db: AsyncSession, session_id: uuid.UUID) -> list[ChatMessage]:
    rows = await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(settings.chat_history_turns * 2)
    )
    return list(reversed(list(rows)))


async def _rewrite(db: AsyncSession, session: ChatSession, history: list[ChatMessage], question: str) -> str:
    if not history:
        return question
    pairs = [(m.role, strip_citations(m.content)) for m in history]
    try:
        result = await get_provider().acomplete(
            [Message("system", prompts.REWRITE_SYSTEM), Message("user", prompts.rewrite_user(pairs, question))],
            model=settings.utility_model,
            purpose="rewrite",
            max_tokens=120,
            temperature=0.0,
        )
    except AIProviderError as exc:
        logger.warning("query rewrite failed; using raw question", extra={"error": str(exc)})
        return question
    db.add(usage_row(owner_id=session.owner_id, operation="rewrite", result=result, session_id=session.id))
    rewritten = result.text.strip().strip('"')
    return rewritten if 0 < len(rewritten) < 1000 else question


async def _has_ready_documents(db: AsyncSession, session: ChatSession) -> bool:
    cond = [Document.owner_id == session.owner_id, Document.status == DocumentStatus.READY]
    if session.document_ids:
        cond.append(Document.id.in_(session.document_ids))
    return bool(await db.scalar(select(exists().where(*cond))))


async def prepare_turn(db: AsyncSession, session: ChatSession, question: str) -> PreparedTurn:
    history = await _history(db, session.id)
    standalone = await _rewrite(db, session, history, question)
    hits, retrieval_ms = await hybrid_search(
        db, standalone, owner_id=session.owner_id, document_ids=session.document_ids or None, session_id=session.id
    )

    sources: dict[str, RetrievedChunk] = {}
    budget = settings.chat_max_context_tokens
    for hit in hits:
        if hit.token_count > budget:
            continue
        budget -= hit.token_count
        sources[f"S{len(sources) + 1}"] = hit

    turn = PreparedTurn(question, standalone, [], sources, retrieval_ms)
    if not sources:
        # No LLM call when there is nothing to ground on: cheaper and can't hallucinate.
        turn.canned_answer = NOT_FOUND if await _has_ready_documents(db, session) else NOT_READY
        return turn

    prompt_sources = [
        {"id": sid, "filename": h.filename, "pages": h.pages, "content": h.content} for sid, h in sources.items()
    ]
    turn.messages = [
        Message("system", prompts.CHAT_SYSTEM),
        # old [S#] markers refer to old source lists; strip them to avoid confusing the model
        *[Message(m.role, strip_citations(m.content)) for m in history],  # type: ignore[arg-type]
        Message("user", prompts.chat_user(prompt_sources, question)),
    ]
    return turn


async def suggest_followups(db: AsyncSession, session: ChatSession, turn: PreparedTurn, answer: str) -> list[str]:
    if not settings.suggest_followups or not extract_citations(answer, turn.sources):
        return []  # ungrounded answer: suggestions would point at content we couldn't find
    try:
        result = await get_provider().acomplete(
            [
                Message("system", prompts.FOLLOWUP_SYSTEM),
                Message(
                    "user",
                    prompts.followup_user(turn.question, answer, [h.content for h in turn.sources.values()][:4]),
                ),
            ],
            model=settings.utility_model,
            purpose="followups",
            json_mode=True,
            max_tokens=200,
            temperature=0.5,
        )
        db.add(usage_row(owner_id=session.owner_id, operation="followups", result=result, session_id=session.id))
        data = json.loads(result.text)
        return [q.strip() for q in data.get("questions", []) if isinstance(q, str) and q.strip()][:3]
    except (AIProviderError, ValueError, AttributeError) as exc:
        logger.info("follow-up suggestion skipped", extra={"error": str(exc)})
        return []


async def persist_turn(
    db: AsyncSession,
    session: ChatSession,
    turn: PreparedTurn,
    *,
    answer: str,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    followups: list[str],
) -> tuple[ChatMessage, ChatMessage]:
    user_msg = ChatMessage(session_id=session.id, role=MessageRole.USER, content=turn.question)
    db.add(user_msg)
    await db.flush()  # clock_timestamp() ordering: user message strictly before the answer
    assistant = ChatMessage(
        session_id=session.id,
        role=MessageRole.ASSISTANT,
        content=answer,
        standalone_query=turn.standalone if turn.standalone != turn.question else None,
        citations=extract_citations(answer, turn.sources),
        suggested_questions=followups,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )
    db.add(assistant)
    values: dict = {"message_count": ChatSession.message_count + 2}  # atomic increment, no lost updates
    if not session.title:
        values["title"] = turn.question[:80]
    await db.execute(update(ChatSession).where(ChatSession.id == session.id).values(**values))
    await db.flush()
    await db.refresh(user_msg)
    await db.refresh(assistant)
    return user_msg, assistant


async def ask(db: AsyncSession, session: ChatSession, question: str) -> dict:
    start = time.perf_counter()
    turn = await prepare_turn(db, session, question)
    completion: Completion | None = None
    if turn.canned_answer:
        answer = turn.canned_answer
    else:
        try:
            completion = await get_provider().acomplete(
                turn.messages, model=settings.chat_model, purpose="chat_answer", max_tokens=900
            )
        except AIProviderError as exc:
            raise UpstreamAIError("The AI provider is unavailable, please retry", details={"reason": str(exc)}) from exc
        db.add(usage_row(owner_id=session.owner_id, operation="chat_answer", result=completion, session_id=session.id))
        answer = completion.text
    followups = await suggest_followups(db, session, turn, answer)
    latency = int((time.perf_counter() - start) * 1000)
    user_msg, assistant = await persist_turn(
        db,
        session,
        turn,
        answer=answer,
        model=completion.model if completion else None,
        prompt_tokens=completion.prompt_tokens if completion else 0,
        completion_tokens=completion.completion_tokens if completion else 0,
        latency_ms=latency,
        followups=followups,
    )
    return {
        "session_id": session.id,
        "question": user_msg,
        "answer": assistant,
        "grounded": bool(assistant.citations),
        "retrieval_ms": turn.retrieval_ms,
        "usage": {
            "prompt_tokens": assistant.prompt_tokens,
            "completion_tokens": assistant.completion_tokens,
            "latency_ms": latency,
        },
    }


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def ask_stream(session_id: uuid.UUID, owner_id: str, question: str) -> AsyncIterator[str]:
    """SSE events: `meta` (sources, before generation) -> `token`* -> `done` | `error`.

    Uses its own DB session: the request-scoped one may be closed while the response streams.
    """
    start = time.perf_counter()
    async with AsyncSessionLocal() as db:
        try:
            session = await db.get(ChatSession, session_id)
            if session is None or session.owner_id != owner_id:
                yield sse("error", {"code": "not_found", "message": "Chat session not found"})
                return
            turn = await prepare_turn(db, session, question)
            yield sse(
                "meta",
                {
                    "session_id": str(session_id),
                    "standalone_query": turn.standalone,
                    "sources": [
                        {"source_id": sid, "filename": h.filename, "pages": h.pages, "document_id": str(h.document_id)}
                        for sid, h in turn.sources.items()
                    ],
                },
            )

            result = StreamResult(model=settings.chat_model)
            if turn.canned_answer:
                answer = turn.canned_answer
                yield sse("token", {"delta": answer})
            else:
                async for delta in get_provider().astream(
                    turn.messages, model=settings.chat_model, purpose="chat_answer", result=result, max_tokens=900
                ):
                    yield sse("token", {"delta": delta})
                answer = result.text
                db.add(usage_row(owner_id=owner_id, operation="chat_answer", result=result, session_id=session_id))

            followups = await suggest_followups(db, session, turn, answer)
            latency = int((time.perf_counter() - start) * 1000)
            _, assistant = await persist_turn(
                db,
                session,
                turn,
                answer=answer,
                model=None if turn.canned_answer else result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=latency,
                followups=followups,
            )
            await db.commit()
            yield sse(
                "done",
                {
                    "message_id": str(assistant.id),
                    "citations": assistant.citations,
                    "suggested_questions": followups,
                    "grounded": bool(assistant.citations),
                    "usage": {
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "first_token_ms": result.first_token_ms,
                        "latency_ms": latency,
                    },
                },
            )
        except AIProviderError as exc:
            await db.rollback()
            yield sse("error", {"code": "ai_provider_error", "message": "The AI provider is unavailable, please retry"})
            logger.warning("stream failed", extra={"error": str(exc)})
        except Exception:
            await db.rollback()
            logger.exception("stream failed")
            yield sse("error", {"code": "internal_error", "message": "An unexpected error occurred"})
