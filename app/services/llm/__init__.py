import uuid
from functools import lru_cache

from app.core.config import settings
from app.models import LLMUsage
from app.services.llm.base import (
    AIProviderError,
    Completion,
    Embeddings,
    LLMProvider,
    Message,
    StreamResult,
)
from app.services.llm.pricing import cost_usd

__all__ = [
    "AIProviderError",
    "Completion",
    "Embeddings",
    "LLMProvider",
    "Message",
    "StreamResult",
    "get_provider",
    "usage_row",
]


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        from app.services.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(
            settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            embedding_dim=settings.embedding_dim,
        )
    from app.services.llm.fake_provider import FakeProvider

    return FakeProvider(settings.embedding_dim)


def usage_row(
    *,
    owner_id: str,
    operation: str,
    result: Completion | Embeddings | StreamResult,
    document_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    cached: bool = False,
) -> LLMUsage:
    """Build the cost-tracking row; caller adds it to its own (sync or async) session/transaction."""
    completion_tokens = getattr(result, "completion_tokens", 0)
    return LLMUsage(
        owner_id=owner_id,
        operation=operation,
        provider=get_provider().name,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=0.0 if cached else cost_usd(result.model, result.prompt_tokens, completion_tokens),
        latency_ms=result.latency_ms,
        cached=cached,
        document_id=document_id,
        session_id=session_id,
    )
