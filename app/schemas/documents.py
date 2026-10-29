import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    file_type: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    analysis_status: str
    error_code: str | None
    error_message: str | None
    attempts: int
    page_count: int | None
    chunk_count: int
    token_count: int | None
    title: str | None
    summary: str | None
    category: str | None
    tags: list[str]
    language: str | None
    version: int
    previous_version_id: uuid.UUID | None
    is_latest: bool
    created_at: datetime
    processed_at: datetime | None
    analyzed_at: datetime | None


class UploadResponse(BaseModel):
    document: DocumentOut
    duplicate: bool = Field(description="True when identical content was already uploaded; no reprocessing happens")
    status_url: str


class BatchItem(BaseModel):
    filename: str | None
    ok: bool
    duplicate: bool = False
    document: DocumentOut | None = None
    error: dict | None = None


class BatchUploadResponse(BaseModel):
    accepted: int
    failed: int
    items: list[BatchItem]


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class InsightsOut(BaseModel):
    document_id: uuid.UUID
    analysis_status: str
    title: str | None
    summary: str | None
    category: str | None
    tags: list[str]
    language: str | None
    key_points: list[str] = []
    sentiment: dict | None = None
    entities: list[dict] = []
    suggested_questions: list[str] = []
    model: str | None = None
    prompt_version: str | None = None


class SummaryRequest(BaseModel):
    length: Literal["short", "medium", "long"] = "medium"
    tone: Literal["neutral", "executive", "technical", "simple"] = "neutral"
    focus: str | None = Field(None, max_length=300, description="e.g. 'payment terms and penalties'")
    bullet_points: bool = False


class SummaryOut(BaseModel):
    document_id: uuid.UUID
    summary: str
    params: SummaryRequest
    cached: bool
    model: str


class CompareRequest(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=2, max_length=5)
    focus: str | None = Field(None, max_length=300)


class CompareOut(BaseModel):
    documents: list[dict]
    overview: str
    similarities: list[str]
    differences: list[dict]
    unique_points: dict[str, list[str]]
    conclusion: str | None
    cached: bool


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_index: int
    content: str
    token_count: int
    page_start: int | None
    page_end: int | None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    document_ids: list[uuid.UUID] | None = None
    top_k: int = Field(8, ge=1, le=50)


class SearchHit(BaseModel):
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    pages: str | None
    content: str
    score: float
    vector_rank: int | None
    keyword_rank: int | None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    took_ms: int
