FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt
# Bake the tokenizer into the image; otherwise the first request downloads it (seconds of latency, fails offline).
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && chmod -R a+rX /opt/tiktoken

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY samples ./samples

RUN useradd --create-home vault && mkdir -p /data/uploads && chown -R vault /data
USER vault

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
