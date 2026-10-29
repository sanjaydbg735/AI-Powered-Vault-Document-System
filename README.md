# Vault – AI-Powered Document System

"ChatGPT for your documents": upload PDF/DOCX/TXT/MD files, get AI insights
(summary, key points, category, tags, sentiment, entities, suggested questions),
and chat with one or many documents using RAG with **source citations**.

- **Docs:** [AI usage](AI_USAGE.md). Setup, API, and design choices are in this README.
- **Stack:** FastAPI (async) · PostgreSQL 16 + pgvector · Celery + Redis · OpenAI (GPT + embeddings) · pypdf / python-docx · pytest

## Setup on a new machine

### Prerequisites

| Tool | Version | Needed for |
|------|---------|------------|
| Git | any | cloning |
| Docker Desktop (Windows/macOS) or Docker Engine + Compose plugin (Linux) | Compose **2.24+** (`docker compose version`) | running everything |
| Python | 3.11+ | only for running tests or the app outside Docker |

No API key is needed to start. The app runs in **offline mode** by default.

### 1. Run with Docker (recommended)

Linux / macOS:

```bash
git clone <your-repo-url> vault && cd vault
cp .env.example .env               # optional: Docker also works without .env (offline mode)
docker compose up -d --build       # postgres + pgvector, redis, api (runs DB migrations), worker
docker compose ps                  # wait until api shows "healthy" (about 20-40 s on first start)
```

Windows (PowerShell, with Docker Desktop running):

```powershell
git clone <your-repo-url> vault; cd vault
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
```

Then open:

- **Demo UI:** <http://localhost:18000/>. Click **Load sample documents**, wait until each file shows **ready**, then click a suggested question.
- **Swagger (all APIs):** <http://localhost:18000/docs>. Each endpoint has an `x-api-key` field. Enter `dev-key` (tenant `demo`) or `second-key` (tenant `alice`).
- **Health check:** <http://localhost:18000/api/v1/health/ready> should return `"status": "ok"`.

Optional scripted demo (Linux/macOS/Git Bash): `./scripts/demo.sh`

The first build downloads about 1 GB of images and Python packages. Later starts take a few seconds.

### 2. Switch to OpenAI (real GPT answers)

1. Edit `.env`:

   ```bash
   LLM_PROVIDER=openai
   OPENAI_API_KEY=sk-...
   ```

2. Restart the app containers: `docker compose up -d api worker`
3. **Re-index documents.** Offline embeddings and OpenAI embeddings are not comparable, so documents indexed in offline mode will not be found by OpenAI searches. Either delete them and upload again, call `POST /api/v1/documents/{id}/reprocess` for each, or start clean:

   ```bash
   docker compose down -v && docker compose up -d --build   # wipes database and uploaded files
   ```

The UI header changes from **Offline mode** to **openai**. Costs then show in **Metrics** (`/api/v1/metrics/usage`).

### 3. Run tests

Tests use a real PostgreSQL (a separate `vault_test` database) and Redis db 15, always in offline mode, so they never call OpenAI.

```bash
docker compose up -d postgres redis
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\pip install -r requirements-dev.txt
.venv/bin/pytest -q                                # Windows: .venv\Scripts\pytest -q
```

If you changed `POSTGRES_PORT` / `REDIS_PORT`, set `TEST_DATABASE_HOST=localhost:<port>` and `TEST_REDIS_URL=redis://localhost:<port>/15`.

### 4. Run the API and worker outside Docker (for development)

```bash
docker compose up -d postgres redis
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 18000 --reload                              # terminal 1
.venv/bin/celery -A app.workers.celery_app worker -Q ingest,analysis -c 4 -l INFO   # terminal 2
```

On Windows, add `--pool=solo` to the Celery command (Celery's default process pool does not run on Windows).

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `port is already allocated` | Set `API_PORT`, `POSTGRES_PORT` or `REDIS_PORT` in `.env` to free ports, then `docker compose up -d`. Update `DATABASE_URL`/`REDIS_URL` in `.env` only if you run outside Docker. |
| Document stuck in `queued` | The worker isn't running: `docker compose logs worker`. |
| Document `failed` with `no_text` | The PDF has no text layer (a scanned image). OCR is not supported. |
| `/health/ready` returns 503 | Postgres or Redis is down: `docker compose ps`, then `docker compose up -d`. |
| `env_file ... required` error | Your Compose is older than 2.24. Update Docker, or run `cp .env.example .env` first. |
| `./scripts/demo.sh: bad interpreter` on Windows | Run it from Git Bash or WSL. The repo forces LF line endings via `.gitattributes`. |
| api/worker exit with `LLM_PROVIDER=openai requires OPENAI_API_KEY` | Put your key in `.env`, or set `LLM_PROVIDER=fake`. |
| Answers are poor after switching to OpenAI | Documents still have offline embeddings. Re-index them (step 2.3). |
| Start completely fresh | `docker compose down -v` removes the database, Redis data and uploaded files. |

> The offline provider (`LLM_PROVIDER=fake`) is deterministic: hashed bag-of-words
> embeddings (retrieval genuinely works lexically) and extractive answers that
> follow the same `[S#]` citation protocol. It exists so tests, CI and demos run
> without a key.

## Architecture

```
client ──HTTP/SSE──▶ FastAPI (async)                     Celery worker (sync)
                     │ auth (X-API-Key → tenant)          │ queue "ingest":   extract → chunk → embed → index
                     │ validation, rate limit             │ queue "analysis": LLM structured analysis
                     │ RAG chat, search, metrics          │ retries w/ backoff, idempotent, per-stage events
                     ▼                                    ▼
             PostgreSQL + pgvector  ◀───────────────────────
             (documents, chunks: vector HNSW + tsvector GIN, chat, processing_events, llm_usage)
             Redis: broker · query-embedding & summary cache · rate-limit counters
             Storage: content-addressed files (local; S3-ready interface)
```

**Document lifecycle.** `queued → processing → ready` (chat-able) → analysis
`pending → processing → completed`. Analysis runs as a separate task on its own
queue, so a document can be chatted with before (or even if) summarisation
completes. Failures carry `error_code` / `error_message`; `POST /documents/{id}/reprocess` retries.

**Chat turn (RAG).**
1. Load the last N turns; if there is history, rewrite the question into a standalone query (`"and the interest?"` → `"late payment interest on invoices"`).
2. **Hybrid retrieval** in one SQL round trip: pgvector cosine ANN + Postgres full-text, fused with Reciprocal Rank Fusion, filtered by tenant and session documents.
3. Pack chunks into a token budget as `S1..Sn` sources (filename + pages).
4. Grounded generation: answer only from sources, cite `[S#]`, refuse when not found. If retrieval returns nothing, no LLM call is made at all.
5. Parse the citations actually used → `citations[]` with document, pages, snippet, score.
6. Suggest 3 follow-up questions (only for grounded answers).
7. Persist both messages and token/cost usage in one transaction.

### Project layout

```
app/
  api/routes/      documents.py chat.py metrics.py health.py   (HTTP layer only)
  core/            config, logging (JSON + request id), errors (envelope), rate_limit
  db/ models/      SQLAlchemy 2.0 models; migrations/ holds the Alembic schema
  schemas/         Pydantic request/response + validated AI output models
  services/        ingestion, file_validation, storage, extraction, chunking,
                   embeddings, retrieval (hybrid), chat (RAG), analysis, document_ai,
                   prompts (all prompts), cache, llm/ (provider interface, OpenAI, fake, pricing)
  workers/         celery_app, tasks (retry policy), pipeline (stage logic, testable)
  static/          demo UI (plain HTML/JS/CSS served at /)
migrations/        Alembic schema (pgvector extension, HNSW + GIN indexes)
tests/             unit + integration (real Postgres/pgvector)
scripts/           demo.sh, make_samples.py
samples/           three demo documents (PDF, DOCX, Markdown) used by the UI and demo.sh
```

## API overview

All endpoints under `/api/v1`, header `X-API-Key: dev-key`. Full schemas at `/docs`.

| Area | Endpoint | Notes |
|------|----------|-------|
| Upload | `POST /documents` | 202 + `Location`; 200 + `duplicate:true` for identical content |
| | `POST /documents/batch` | 207, per-file success/failure |
| Read | `GET /documents` | filters `status, category, tag, q`, `sort`, `limit/offset`, `include_old_versions` |
| | `GET /documents/{id}` | status, counts, errors, version info |
| | `GET /documents/{id}/insights` | summary, key points, sentiment, entities, tags, suggested questions |
| | `GET /documents/{id}/chunks` | inspect what was indexed |
| AI | `POST /documents/{id}/summaries` | `{length, tone, focus, bullet_points}` – cached |
| | `POST /documents/compare` | 2–5 docs: similarities, differences, unique points |
| | `POST /search` | hybrid search with `vector_rank` / `keyword_rank` per hit |
| Manage | `POST /documents/{id}/reprocess`, `DELETE /documents/{id}` | |
| Chat | `POST /chat/sessions` | `{document_ids?: [...]}` – empty = all documents |
| | `POST /chat/sessions/{id}/messages` | `{question, stream?}` → JSON or SSE |
| | `GET /chat/sessions/{id}/messages?before=&limit=` | cursor-paginated history |
| | `GET /chat/sessions`, `GET/DELETE /chat/sessions/{id}` | |
| Metrics | `GET /metrics/documents` | counts by status/category/type, top tags, uploads/day |
| | `GET /metrics/processing?window_hours=` | per-stage p50/p95/max, success rate, retries, e2e latency, queue depth, recent failures |
| | `GET /metrics/usage?window_hours=` | tokens & USD cost by operation/model, cache hits, chat p50/p95, grounded rate |
| Health | `GET /health/live`, `GET /health/ready` | readiness checks DB + Redis (503 when degraded) |

### Example

```bash
H='X-API-Key: dev-key'; API=http://localhost:18000/api/v1
curl -H "$H" -F file=@samples/services_agreement.md $API/documents
SID=$(curl -s -H "$H" -H 'Content-Type: application/json' -d '{}' $API/chat/sessions | jq -r .id)
curl -s -H "$H" -H 'Content-Type: application/json' \
  -d '{"question":"How much notice is needed to terminate?"}' $API/chat/sessions/$SID/messages | jq .answer
# streaming
curl -N -H "$H" -H 'Content-Type: application/json' \
  -d '{"question":"Summarise the payment terms","stream":true}' $API/chat/sessions/$SID/messages
```

```json
{
  "content": "Either party may terminate this agreement for convenience with 30 days written notice [S1].",
  "citations": [{"source_id": "S1", "filename": "services_agreement.md", "pages": null,
                 "chunk_index": 0, "snippet": "# Master Services Agreement ...", "score": 0.0328}],
  "suggested_questions": ["What happens on material breach?", "..."]
}
```

Errors always look like:
`{"error": {"code": "unsupported_media_type", "message": "...", "details": {...}}, "request_id": "4ad65d1c..."}`

## Prompt engineering

All prompts live in [`app/services/prompts.py`](app/services/prompts.py) with a `PROMPT_VERSION` stored alongside every analysis.

- **Analysis**: one JSON-mode call returns every insight field (5 tasks for the price of 1). Fixed category taxonomy so it's filterable; explicit rules against invented numbers; output validated by Pydantic with one automatic *repair* retry that shows the model its validation error. Long documents: map (parallel section notes that must keep names/numbers/dates) → reduce.
- **Chat**: numbered `<source id="S1" document=".." pages="..">` blocks; rules for inline citation, explicit refusal text, conflict reporting, exact figures; history is "context only, facts must come from sources". Old `[S#]` markers are stripped from history so they can't be confused with the new turn's sources.
- **Query rewrite**: resolves pronouns, keeps entities/numbers, must not answer – `temperature=0`.
- **Custom summaries**: length/tone/focus are mapped to concrete instructions (e.g. *executive* = "lead with decision/impact"); if the focus topic isn't covered the model must say so.
- **Injection guard**: every prompt states that text inside document/source tags is data, never instructions.

## Scale & performance (how 1000s of documents are handled)

- Upload is O(1) for the API: stream-to-disk + hash, then enqueue. Heavy work is in horizontally scalable workers; `ingest` and `analysis` queues scale independently (`worker_prefetch_multiplier=1`, `acks_late`).
- HNSW index (`m=16, ef_construction=64`) + GIN on `tsvector`; tenant/document filters run inside the ANN query with pgvector iterative scans so filtered queries don't return short result sets.
- Embeddings are batched (64 per request); chunk inserts are a single multi-row `INSERT`.
- Caching: query embeddings, custom summaries and comparisons (content-addressed keys). Duplicate uploads cost no AI calls.
- Cost control: cheap model for rewrite/follow-ups, map-reduce caps analysis input (max 12 sections), no LLM call on empty retrieval, per-tenant rate limits for chat/upload/AI.

## Assumptions & limitations

- Authentication is an API key mapped to a tenant, configured by `API_KEYS` in `.env` (user accounts and OAuth are out of scope). Every query is scoped to that tenant.
- Scanned PDFs (images only) fail with `no_text`; OCR (e.g. Tesseract/Textract) would be a new extractor.
- DOCX has no reliable page numbers, so its citations carry chunk index but no pages.
- Local disk storage; the `Storage` interface is ready for S3.
- English full-text configuration; embeddings themselves are multilingual.

## Possible next steps

Cross-encoder re-ranking, OCR, S3 storage, JWT/OAuth, per-tenant cost quotas
(data already in `llm_usage`), WebSocket push for processing status, a full React frontend.
