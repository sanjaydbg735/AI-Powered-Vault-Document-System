import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    title: str | None = Field(None, max_length=256)
    document_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=50, description="Scope the chat to these documents; empty = all your documents"
    )


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    document_ids: list[uuid.UUID]
    message_count: int
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    source_id: str = Field(description="Marker used in the answer, e.g. S1")
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    pages: str | None
    snippet: str
    score: float


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    standalone_query: str | None = None
    citations: list[Citation] = []
    suggested_questions: list[str] = []
    model: str | None = None
    latency_ms: int | None = None
    created_at: datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    stream: bool = Field(False, description="Stream the answer as Server-Sent Events")


class AskResponse(BaseModel):
    session_id: uuid.UUID
    question: MessageOut
    answer: MessageOut
    grounded: bool = Field(description="True when the answer cites at least one source")
    retrieval_ms: int
    usage: dict


class MessagePage(BaseModel):
    items: list[MessageOut]
    has_more: bool
