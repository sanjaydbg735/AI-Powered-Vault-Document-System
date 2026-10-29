from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.cache import aget_json, aset_json, cache_key
from app.services.llm import Embeddings, get_provider, usage_row


def embed_documents_sync(
    db: Session, texts: list[str], *, owner_id: str, document_id
) -> list[list[float]]:
    """Batch-embed chunk texts in the worker; one usage row per batch."""
    provider = get_provider()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), settings.embedding_batch_size):
        batch = texts[i : i + settings.embedding_batch_size]
        result = provider.embed(batch, model=settings.embedding_model)
        vectors.extend(result.vectors)
        db.add(usage_row(owner_id=owner_id, operation="embed_document", result=result, document_id=document_id))
    return vectors


async def embed_query(db: AsyncSession, text: str, *, owner_id: str, session_id=None) -> list[float]:
    """Query embeddings are cached: identical questions (common in demos, FAQs, retries) cost nothing."""
    key = cache_key("emb", settings.llm_provider, settings.embedding_model, settings.embedding_dim, text.strip().lower())
    if (cached := await aget_json(key)) is not None:
        db.add(
            usage_row(
                owner_id=owner_id,
                operation="embed_query",
                result=Embeddings([], cached["model"], 0, 0),
                session_id=session_id,
                cached=True,
            )
        )
        return cached["vector"]
    result = await get_provider().aembed([text], model=settings.embedding_model)
    db.add(usage_row(owner_id=owner_id, operation="embed_query", result=result, session_id=session_id))
    await aset_json(key, {"vector": result.vectors[0], "model": result.model})
    return result.vectors[0]
