# AI Usage

## 1. AI coding tools used to build this

The project was built with **Cursor (agent mode)**. The workflow:

1. **Plan first.** I asked the agent to read the case-study PDF and write a
   phased plan and a decision log (alternatives, then the choice) *before*
   writing code. Work was ordered so every phase ends in a runnable state:
   foundation → AI layer → upload → pipeline → APIs → chat → metrics → tests → docs.
2. **Implement phase by phase**, verifying each phase against real infrastructure
   (Postgres+pgvector and Redis in Docker), not just by reading code:
   - after the migration: `alembic check` to prove models and migration don't drift;
   - after the AI layer: a smoke script checking that related texts embed closer than unrelated ones;
   - after the pipeline: running ingest twice to prove idempotency;
   - after the API: curl-driven flows for upload, duplicates, chat, SSE and error envelopes.
3. **Tests as the safety net** – 52 unit and integration tests against real Postgres.

### Where the AI tool helped most
- Boilerplate at speed: SQLAlchemy models + hand-written Alembic migration, Pydantic schemas, FastAPI routers.
- Writing the hybrid-search SQL (CTEs, `FULL OUTER JOIN`, RRF) and metrics SQL (`percentile_cont`, `FILTER`).
- Generating a minimal valid PDF writer (`scripts/make_samples.py`) so no extra library was needed for demo data.
- Writing broad test coverage quickly, including failure paths (transient vs. permanent errors, tenant isolation, rate limits).

### Where human-style review / verification was essential (bugs caught)
- **Regex bug in the offline provider:** `<source` also matched `<sources>`, leaking markup into answers. Caught by the smoke test and fixed.
- **Chunker:** a meaningless final-flush condition and O(n²) token counting in word splitting – rewritten.
- **Python version:** PEP 695 generics were generated, but the runtime is 3.11 → switched to `TypeVar`.
- **Celery eager mode** doesn't apply to `send_task`, so the config flag would have been misleading; it was removed, and tests inline the pipeline instead.
- **Performance regression found with our own metrics:** the `/metrics/processing` endpoint showed chunking at a 3.8 s p95 in Docker. The cause was tiktoken downloading its tokenizer at runtime; it's now baked into the image (upload→ready p50: 3.6 s → 0.48 s).
- **Product logic:** follow-up questions were suggested even for "not found" answers; they're now restricted to grounded answers.

## 2. AI inside the product

| Feature | Model use | Design notes |
|---------|-----------|--------------|
| Document analysis | 1 JSON-mode call (+map-reduce for long docs) | title, summary, key points, category (taxonomy), tags, sentiment, entities, language, suggested questions; Pydantic validation + repair retry |
| Embeddings | `text-embedding-3-small`, batched | stored in pgvector (HNSW, cosine) |
| Chat (RAG) | rewrite → hybrid retrieval → grounded answer → follow-ups | `[S#]` citations parsed into structured citations; refusal when unsupported |
| Custom summary | on demand: length / tone / focus / bullets | cached by content hash + params |
| Comparison | 2–5 documents, JSON | similarities, per-document differences, unique points |
| Streaming | SSE | `meta` (sources first) → `token`s → `done` (citations, usage) |

**Automation that reduces manual work:** auto-categorisation and tagging (filterable
in `GET /documents?category=&tag=`), automatic titles, suggested questions per document
(a starting point for users who don't know what to ask), follow-up suggestions after each answer, and
duplicate detection that avoids paying for the same file twice.

**Cost awareness:** every model call writes a row to `llm_usage` (tokens, latency, USD
via a pricing table); `/metrics/usage` reports cost by operation and model, cache hit
counts and average cost per document.

See the "Prompt engineering" section of the README for the reasoning behind each prompt.
