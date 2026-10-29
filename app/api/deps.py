import hmac
import uuid
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError, UnauthorizedError
from app.db.session import get_db
from app.models import ChatSession, Document

DB = Annotated[AsyncSession, Depends(get_db)]


def get_owner(x_api_key: Annotated[str | None, Header(description="API key; maps to a tenant/owner")] = None) -> str:
    if not x_api_key:
        raise UnauthorizedError("Missing X-API-Key header")
    for key, owner in settings.api_key_map.items():
        if hmac.compare_digest(key, x_api_key):  # constant time
            return owner
    raise UnauthorizedError("Invalid API key")


Owner = Annotated[str, Depends(get_owner)]


async def owned_document(db: AsyncSession, owner_id: str, document_id: uuid.UUID) -> Document:
    # 404 (not 403) for other tenants' ids: don't leak existence.
    doc = await db.scalar(select(Document).where(Document.id == document_id, Document.owner_id == owner_id))
    if doc is None:
        raise NotFoundError(f"Document {document_id} not found")
    return doc


async def owned_session(db: AsyncSession, owner_id: str, session_id: uuid.UUID) -> ChatSession:
    s = await db.scalar(select(ChatSession).where(ChatSession.id == session_id, ChatSession.owner_id == owner_id))
    if s is None:
        raise NotFoundError(f"Chat session {session_id} not found")
    return s
