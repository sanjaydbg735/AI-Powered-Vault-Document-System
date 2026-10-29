"""Deterministic, offline provider.

Purpose: run the whole system (tests, CI, demos) without an API key while
exercising the *real* pipeline: embeddings are hashed bag-of-words vectors, so
vector search genuinely ranks lexically similar chunks higher; answers are
extractive and cite sources with the same [S#] protocol as the real model.
"""

import asyncio
import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import AsyncIterator

from app.services.llm.base import Completion, Embeddings, LLMProvider, Message, StreamResult
from app.services.prompts import CATEGORIES
from app.services.tokens import count_tokens

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']+|\d+(?:[.,]\d+)?")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
STOPWORDS = frozenset(
    """a an and are as at be been being but by can could did do does doing for from had has have having he her
    here hers him his how i if in into is it its itself just me more most my no nor not of off on once only or
    other our ours out over own same she should so some such than that the their theirs them then there these they
    this those through to too under until up very was we were what when where which while who whom why will with
    would you your yours about above after again against all am any because before below between both down during
    each few further s t don now also may shall must per via""".split()
)
_CATEGORY_HINTS = {
    "legal": {"agreement", "contract", "party", "parties", "clause", "liability", "termination", "law", "court"},
    "finance": {"revenue", "invoice", "payment", "budget", "profit", "cost", "tax", "financial", "usd", "price"},
    "technical": {"system", "api", "software", "server", "architecture", "database", "code", "deployment"},
    "medical": {"patient", "diagnosis", "treatment", "clinical", "medical", "dose", "symptoms"},
    "hr": {"employee", "leave", "salary", "hiring", "benefits", "onboarding", "vacation", "employees"},
    "marketing": {"campaign", "brand", "customers", "market", "marketing", "audience", "sales"},
    "research": {"study", "results", "hypothesis", "experiment", "analysis", "findings", "method"},
    "policy": {"policy", "compliance", "must", "procedure", "security", "requirements", "guidelines"},
    "education": {"course", "students", "learning", "curriculum", "assignment", "exam"},
}


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS and len(w) > 2]


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENT.split(flat) if len(s.strip()) > 20]


def keywords(text: str, n: int) -> list[str]:
    return [w for w, _ in Counter(content_words(text)).most_common(n)]


def _tag(text: str, name: str) -> list[tuple[str, str]]:
    return re.findall(rf"<{name}((?:\s[^>]*)?)>\n?(.*?)\n?</{name}>", text, flags=re.S)


def _attr(attrs: str, key: str) -> str | None:
    m = re.search(rf'{key}="([^"]*)"', attrs)
    return m.group(1) if m else None


def hashed_embedding(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    toks = content_words(text)
    feats = Counter(toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])])
    for feat, tf in feats.items():
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
        vec[h % dim] += (1.0 if (h >> 63) & 1 else -1.0) * (1 + math.log(tf))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, embedding_dim: int):
        self.dim = embedding_dim

    # ------------------------------------------------------------------ behaviours
    def _respond(self, messages: list[Message], purpose: str) -> str:
        user = messages[-1].content
        handler = getattr(self, f"_do_{purpose}", None)
        return handler(user) if handler else "OK"

    def _do_rewrite(self, user: str) -> str:
        q = _tag(user, "question")
        return q[0][1].strip() if q else user

    def _do_chat_answer(self, user: str) -> str:
        question = (_tag(user, "question") or [("", user)])[0][1]
        q_terms = set(content_words(question))
        scored: list[tuple[float, str, str]] = []
        for attrs, body in _tag(user, "source"):
            sid = _attr(attrs, "id") or "S1"
            for sent in sentences(body) or [body.strip()]:
                overlap = len(q_terms & set(content_words(sent)))
                if overlap:
                    scored.append((overlap / math.sqrt(len(content_words(sent)) + 1), sid, sent))
        if not scored:
            return "I couldn't find that in your documents."
        scored.sort(key=lambda x: -x[0])
        picked, seen = [], set()
        for _, sid, sent in scored:
            if sent not in seen:
                seen.add(sent)
                picked.append(f"{sent} [{sid}]")
            if len(picked) == 3:
                break
        return "Based on your documents:\n\n" + "\n".join(f"- {p}" for p in picked)

    def _do_followups(self, user: str) -> str:
        question = (_tag(user, "question") or [("", "")])[0][1]
        asked = set(content_words(question))
        kws = [k for k in keywords(user, 12) if k not in asked and k not in {"question", "answer", "sources"}]
        templates = ["What does the document say about {}?", "Are there any details on {}?", "How is {} described?"]
        qs = [t.format(k) for t, k in zip(templates, kws)]
        return json.dumps({"questions": qs})

    def _do_section_notes(self, user: str) -> str:
        body = (_tag(user, "section") or [("", user)])[0][1]
        return "\n".join(f"- {s}" for s in self._top_sentences(body, 5))

    def _do_analysis(self, user: str) -> str:
        attrs, body = (_tag(user, "document") or [("", user)])[0]
        filename = _attr(attrs, "filename") or "document"
        sents = sentences(body)
        kws = keywords(body, 8)
        first_line = next((ln.strip(" -#*") for ln in body.splitlines() if len(ln.strip()) > 3), filename)
        ents = Counter(m for m in re.findall(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b", body))
        return json.dumps(
            {
                "title": " ".join(first_line.split()[:12]),
                "summary": " ".join(sents[:3]) or body[:400],
                "key_points": self._top_sentences(body, 5),
                "category": self._category(body),
                "tags": kws[:6],
                "sentiment": {"label": "neutral", "score": 0.0},
                "entities": [{"name": n, "type": "other"} for n, _ in ents.most_common(8)],
                "language": "en",
                "suggested_questions": [f"What does the document say about {k}?" for k in kws[:4]],
            }
        )

    def _do_summary(self, user: str) -> str:
        body = (_tag(user, "document") or [("", user)])[0][1]
        return " ".join(self._top_sentences(body, 4)) or body[:500]

    def _do_compare(self, user: str) -> str:
        docs = [(_attr(a, "id") or "?", set(keywords(b, 25)), b) for a, b in _tag(user, "document")]
        common = set.intersection(*(d[1] for d in docs)) if docs else set()
        return json.dumps(
            {
                "overview": f"Compared {len(docs)} documents; shared themes: {', '.join(sorted(common)[:6]) or 'none'}.",
                "similarities": [f"Both discuss {k}." for k in sorted(common)[:5]],
                "differences": [],
                "unique_points": {d[0]: sorted(d[1] - common)[:5] for d in docs},
                "conclusion": "Offline comparison based on keyword overlap.",
            }
        )

    @staticmethod
    def _top_sentences(text: str, n: int) -> list[str]:
        sents = sentences(text)
        freq = Counter(content_words(text))
        scored = sorted(
            range(len(sents)),
            key=lambda i: -sum(freq[w] for w in content_words(sents[i])) / (len(content_words(sents[i])) + 1),
        )
        return [sents[i] for i in sorted(scored[:n])]

    @staticmethod
    def _category(text: str) -> str:
        toks = Counter(words(text))
        scores = {c: sum(toks[w] for w in hints) for c, hints in _CATEGORY_HINTS.items()}
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 and best in CATEGORIES else "other"

    # ------------------------------------------------------------------ interface
    def _completion(self, messages: list[Message], purpose: str, start: float) -> Completion:
        text = self._respond(messages, purpose)
        prompt_tokens = sum(count_tokens(m.content) for m in messages)
        return Completion(text, "fake", prompt_tokens, count_tokens(text), int((time.perf_counter() - start) * 1000))

    def complete(self, messages, *, model, purpose, temperature=0.2, max_tokens=1024, json_mode=False):
        return self._completion(messages, purpose, time.perf_counter())

    async def acomplete(self, messages, *, model, purpose, temperature=0.2, max_tokens=1024, json_mode=False):
        return self._completion(messages, purpose, time.perf_counter())

    async def astream(
        self, messages, *, model, purpose, result: StreamResult, temperature=0.2, max_tokens=1024
    ) -> AsyncIterator[str]:
        start = time.perf_counter()
        comp = self._completion(messages, purpose, start)
        for piece in re.findall(r"\S+\s*", comp.text):
            if result.first_token_ms is None:
                result.first_token_ms = int((time.perf_counter() - start) * 1000)
            yield piece
            await asyncio.sleep(0)
        result.text, result.model = comp.text, "fake"
        result.prompt_tokens, result.completion_tokens = comp.prompt_tokens, comp.completion_tokens
        result.latency_ms = int((time.perf_counter() - start) * 1000)

    def embed(self, texts, *, model):
        start = time.perf_counter()
        vecs = [hashed_embedding(t, self.dim) for t in texts]
        return Embeddings(vecs, "fake", sum(count_tokens(t) for t in texts), int((time.perf_counter() - start) * 1000))

    async def aembed(self, texts, *, model):
        return self.embed(texts, model=model)
