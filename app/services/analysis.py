"""AI document analysis (runs in the worker).

One structured call produces title/summary/key points/category/tags/sentiment/
entities/suggested questions. Long documents use map-reduce: sections are
condensed into notes in parallel, then the notes are analysed – bounded cost and
no context-window overflow regardless of document length.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.schemas.ai import DocumentAnalysis
from app.services import prompts
from app.services.chunking import chunk_pages
from app.services.extraction import Page
from app.services.llm import Message, get_provider, usage_row
from app.services.tokens import count_tokens

logger = logging.getLogger(__name__)

MAX_SECTIONS = 12

T = TypeVar("T", bound=BaseModel)


def parse_json_model(text: str, model: type[T]) -> T:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return model.model_validate(json.loads(text[start : end + 1]))


def complete_json_sync(
    db: Session, messages: list[Message], model_cls: type[T], *, purpose: str, owner_id: str, document_id=None
) -> T:
    """JSON-mode call with one repair attempt if the output fails validation."""
    provider = get_provider()
    for attempt in range(2):
        result = provider.complete(
            messages, model=settings.analysis_model, purpose=purpose, json_mode=True, max_tokens=1500, temperature=0.1
        )
        db.add(usage_row(owner_id=owner_id, operation=purpose, result=result, document_id=document_id))
        try:
            return parse_json_model(result.text, model_cls)
        except (ValueError, ValidationError) as exc:
            logger.warning("invalid JSON from model", extra={"purpose": purpose, "attempt": attempt, "error": str(exc)})
            messages = messages + [
                Message("assistant", result.text),
                Message("user", f"That output was invalid ({str(exc)[:300]}). Return only valid JSON matching the schema."),
            ]
    raise ValueError(f"model returned invalid JSON for {purpose}")


def _sample_sections(sections: list[str]) -> list[str]:
    if len(sections) <= MAX_SECTIONS:
        return sections
    step = len(sections) / MAX_SECTIONS
    return [sections[int(i * step)] for i in range(MAX_SECTIONS)]


def analyze_pages(db: Session, pages: list[Page], *, filename: str, owner_id: str, document_id) -> DocumentAnalysis:
    full_text = "\n\n".join(p.text for p in pages if p.text)
    from_notes = False
    if count_tokens(full_text) > settings.analysis_max_input_tokens:
        sections = [c.content for c in chunk_pages(pages, size=settings.analysis_section_tokens, overlap=200)]
        sections = _sample_sections(sections)
        provider = get_provider()

        def notes(i_text: tuple[int, str]):
            i, text = i_text
            return provider.complete(
                [Message("system", prompts.SECTION_NOTES_SYSTEM), Message("user", prompts.section_user(i + 1, len(sections), text))],
                model=settings.analysis_model,
                purpose="section_notes",
                max_tokens=400,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(notes, enumerate(sections)))
        for r in results:
            db.add(usage_row(owner_id=owner_id, operation="section_notes", result=r, document_id=document_id))
        full_text = "\n\n".join(r.text for r in results)
        from_notes = True

    messages = [
        Message("system", prompts.ANALYSIS_SYSTEM),
        Message("user", prompts.analysis_user(filename, full_text, from_notes=from_notes)),
    ]
    return complete_json_sync(
        db, messages, DocumentAnalysis, purpose="analysis", owner_id=owner_id, document_id=document_id
    )
