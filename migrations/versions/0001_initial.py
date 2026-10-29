"""initial schema: documents, chunks (pgvector + FTS), chat, metrics

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

from app.core.config import settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("analysis_status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer),
        sa.Column("char_count", sa.Integer),
        sa.Column("token_count", sa.Integer),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("title", sa.String(512)),
        sa.Column("summary", sa.Text),
        sa.Column("insights", pg.JSONB),
        sa.Column("category", sa.String(64)),
        sa.Column("tags", pg.ARRAY(sa.String(64)), nullable=False, server_default="{}"),
        sa.Column("language", sa.String(16)),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "previous_version_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL", name="fk_documents_previous_version_id_documents"),
        ),
        sa.Column("is_latest", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("analyzed_at", sa.DateTime(timezone=True)),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("owner_id", "sha256", name="uq_documents_owner_sha256"),
        sa.CheckConstraint("status IN ('queued','processing','ready','failed')", name="ck_documents_status"),
        sa.CheckConstraint(
            "analysis_status IN ('pending','processing','completed','failed')", name="ck_documents_analysis_status"
        ),
    )
    op.create_index("ix_documents_owner_created", "documents", ["owner_id", "created_at"])
    op.create_index("ix_documents_owner_filename", "documents", ["owner_id", "filename"])
    op.create_index("ix_documents_category", "documents", ["category"])
    op.create_index("ix_documents_tags", "documents", ["tags"], postgresql_using="gin")

    op.create_table(
        "document_chunks",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE", name="fk_document_chunks_document_id_documents"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("page_start", sa.Integer),
        sa.Column("page_end", sa.Integer),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("embedding", Vector(settings.embedding_dim), nullable=False),
        sa.Column("tsv", pg.TSVECTOR, sa.Computed("to_tsvector('english', content)", persisted=True)),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_index"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index("ix_document_chunks_owner_id", "document_chunks", ["owner_id"])
    op.create_index("ix_chunks_tsv", "document_chunks", ["tsv"], postgresql_using="gin")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256)),
        sa.Column("document_ids", pg.ARRAY(pg.UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_chat_sessions_owner_updated", "chat_sessions", ["owner_id", "updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE", name="fk_chat_messages_session_id_chat_sessions"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("standalone_query", sa.Text),
        sa.Column("citations", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("suggested_questions", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("model", sa.String(64)),
        sa.Column("prompt_tokens", sa.Integer),
        sa.Column("completion_tokens", sa.Integer),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
    )
    op.create_index("ix_chat_messages_session_created", "chat_messages", ["session_id", "created_at"])

    op.create_table(
        "processing_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL", name="fk_processing_events_document_id_documents"),
        ),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("error", sa.Text),
        _ts("created_at"),
    )
    op.create_index("ix_processing_events_stage_created", "processing_events", ["stage", "created_at"])
    op.create_index("ix_processing_events_document", "processing_events", ["document_id"])

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "document_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL", name="fk_llm_usage_document_id_documents"),
        ),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL", name="fk_llm_usage_session_id_chat_sessions"),
        ),
        _ts("created_at"),
    )
    op.create_index("ix_llm_usage_owner_created", "llm_usage", ["owner_id", "created_at"])
    op.create_index("ix_llm_usage_operation", "llm_usage", ["operation"])


def downgrade() -> None:
    for table in ("llm_usage", "processing_events", "chat_messages", "chat_sessions", "document_chunks", "documents"):
        op.drop_table(table)
