from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Vault Document AI"
    environment: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # Infrastructure
    database_url: str = "postgresql+asyncpg://vault:vault@localhost:5432/vault"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Auth: comma separated "key:owner" pairs, e.g. "demo-key:alice,other:bob"
    api_keys: str = "dev-key:demo"

    # Storage
    storage_dir: Path = Path("./data/uploads")
    max_upload_mb: int = 25
    max_batch_files: int = 20

    # AI provider
    llm_provider: Literal["openai", "fake"] = "fake"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    chat_model: str = "gpt-4o-mini"
    analysis_model: str = "gpt-4o-mini"
    utility_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3

    # Chunking / retrieval
    chunk_size_tokens: int = 500
    chunk_overlap_tokens: int = 80
    embedding_batch_size: int = 64
    retrieval_top_k: int = 6
    retrieval_candidates: int = 30
    rrf_k: int = 60
    chat_history_turns: int = 6
    chat_max_context_tokens: int = 6000
    suggest_followups: bool = True

    # Analysis
    analysis_max_input_tokens: int = 12000
    analysis_section_tokens: int = 6000

    # Rate limits (requests per minute per owner)
    rate_limit_enabled: bool = True
    rate_limit_chat_per_min: int = 30
    rate_limit_upload_per_min: int = 30
    rate_limit_ai_per_min: int = 30

    # Cache
    cache_ttl_s: int = 60 * 60 * 24 * 7

    @field_validator("database_url")
    @classmethod
    def _ensure_async_driver(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    @model_validator(mode="after")
    def _require_openai_key(self) -> "Settings":
        # Fail at startup, not on the first upload or chat message.
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY (or set LLM_PROVIDER=fake for offline mode)")
        return self

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def api_key_map(self) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for item in self.api_keys.split(","):
            item = item.strip()
            if not item:
                continue
            key, _, owner = item.partition(":")
            pairs[key.strip()] = (owner or key).strip()
        return pairs


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
