"""On-demand AI features called from the API (async): custom summaries and comparisons.

Results are cached by *content hash* + parameters + model + prompt version, so
an identical request is free and a new document version can never hit a stale entry.
"""

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import DocumentNotReadyError, UpstreamAIError
from app.models import Document, DocumentStatus
from app.schemas.ai import CompareResult
from app.schemas.documents import SummaryRequest
from app.services import prompts
from app.services.analysis import parse_json_model
from app.services.cache import aget_json, aset_json, cache_key
from app.services.extraction import extract
from app.services.llm import AIProviderError, Message, get_provider, usage_row
from app.services.storage import get_storage
from app.services.tokens import truncate_to_tokens


def _require_ready(doc: Document) -> None:
    if doc.status != DocumentStatus.READY:
        raise DocumentNotReadyError(
            f"Document '{doc.filename}' is {doc.status}; wait until status is 'ready'",
            details={"document_id": str(doc.id), "status": doc.status},
        )


async def _document_text(doc: Document, max_tokens: int) -> str:
    pages = await run_in_threadpool(extract, get_storage().local_path(doc.storage_key), doc.file_type)
    return truncate_to_tokens("\n\n".join(p.text for p in pages), max_tokens)


async def custom_summary(db: AsyncSession, doc: Document, params: SummaryRequest) -> tuple[str, bool, str]:
    _require_ready(doc)
    key = cache_key("summary", doc.sha256, params.model_dump(), settings.analysis_model, prompts.PROMPT_VERSION)
    if cached := await aget_json(key):
        return cached["summary"], True, cached["model"]

    text = await _document_text(doc, settings.analysis_max_input_tokens)
    messages = [
        Message("system", prompts.custom_summary_system(params.length, params.tone, params.focus, params.bullet_points)),
        Message("user", prompts.analysis_user(doc.filename, text)),
    ]
    try:
        result = await get_provider().acomplete(
            messages, model=settings.analysis_model, purpose="summary", max_tokens=900, temperature=0.3
        )
    except AIProviderError as exc:
        raise UpstreamAIError("AI provider failed to generate the summary", details={"reason": str(exc)}) from exc
    db.add(usage_row(owner_id=doc.owner_id, operation="summary", result=result, document_id=doc.id))
    await aset_json(key, {"summary": result.text, "model": result.model})
    return result.text, False, result.model


async def compare_documents(db: AsyncSession, docs: list[Document], focus: str | None) -> tuple[CompareResult, bool]:
    for d in docs:
        _require_ready(d)
    key = cache_key("compare", sorted(d.sha256 for d in docs), focus, settings.analysis_model, prompts.PROMPT_VERSION)
    if cached := await aget_json(key):
        return CompareResult.model_validate(cached), True

    per_doc_budget = settings.analysis_max_input_tokens // len(docs)
    texts = [(str(d.id), d.filename, await _document_text(d, per_doc_budget)) for d in docs]
    messages = [Message("system", prompts.COMPARE_SYSTEM), Message("user", prompts.compare_user(texts, focus))]
    provider = get_provider()
    for attempt in range(2):
        try:
            result = await provider.acomplete(
                messages, model=settings.analysis_model, purpose="compare", json_mode=True, max_tokens=1500
            )
        except AIProviderError as exc:
            raise UpstreamAIError("AI provider failed to compare documents", details={"reason": str(exc)}) from exc
        db.add(usage_row(owner_id=docs[0].owner_id, operation="compare", result=result))
        try:
            parsed = parse_json_model(result.text, CompareResult)
            await aset_json(key, parsed.model_dump())
            return parsed, False
        except (ValueError, ValidationError) as exc:
            messages = messages + [
                Message("assistant", result.text),
                Message("user", f"Invalid output ({str(exc)[:200]}). Return only valid JSON matching the schema."),
            ]
    raise UpstreamAIError("AI provider returned an invalid comparison")
