"""Token-aware recursive chunking with overlap that preserves page provenance.

Paragraphs are the preferred unit; oversized paragraphs fall back to sentences,
then to word windows. Segments are packed greedily up to `chunk_size` tokens and
each new chunk starts with the tail of the previous one (`overlap` tokens) so an
answer spanning a boundary is still retrievable.
"""

import re
from dataclasses import dataclass

from app.services.extraction import Page
from app.services.tokens import count_tokens

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Segment:
    text: str
    tokens: int
    page: int | None


@dataclass(slots=True)
class Chunk:
    index: int
    content: str
    token_count: int
    page_start: int | None
    page_end: int | None


def _split_words(text: str, size: int, page: int | None) -> list[Segment]:
    out: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for w in text.split():
        n = count_tokens(" " + w)
        if cur and cur_tokens + n > size:
            out.append(" ".join(cur))
            cur, cur_tokens = [], 0
        cur.append(w)
        cur_tokens += n
    if cur:
        out.append(" ".join(cur))
    return [Segment(t, count_tokens(t), page) for t in out]


def _segments(pages: list[Page], size: int) -> list[Segment]:
    segs: list[Segment] = []
    for page in pages:
        for para in re.split(r"\n\s*\n", page.text):
            para = para.strip()
            if not para:
                continue
            n = count_tokens(para)
            if n <= size:
                segs.append(Segment(para, n, page.number))
                continue
            for sent in _SENTENCE.split(para):
                sent = sent.strip()
                if not sent:
                    continue
                m = count_tokens(sent)
                segs.extend([Segment(sent, m, page.number)] if m <= size else _split_words(sent, size, page.number))
    return segs


def chunk_pages(pages: list[Page], size: int = 500, overlap: int = 80) -> list[Chunk]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than chunk size")
    chunks: list[Chunk] = []
    window: list[Segment] = []
    window_tokens = 0

    def flush() -> None:
        pages_in = [s.page for s in window if s.page is not None]
        content = "\n\n".join(s.text for s in window)
        chunks.append(
            Chunk(
                index=len(chunks),
                content=content,
                token_count=count_tokens(content),
                page_start=min(pages_in) if pages_in else None,
                page_end=max(pages_in) if pages_in else None,
            )
        )

    for seg in _segments(pages, size):
        if window and window_tokens + seg.tokens > size:
            flush()
            # carry the tail of the previous chunk as overlap
            tail: list[Segment] = []
            tail_tokens = 0
            for prev in reversed(window):
                if tail_tokens + prev.tokens > overlap:
                    break
                tail.insert(0, prev)
                tail_tokens += prev.tokens
            window, window_tokens = tail, tail_tokens
        window.append(seg)
        window_tokens += seg.tokens
    # After every flush a new segment is appended, so a non-empty window always holds unseen text.
    if window:
        flush()
    return chunks
