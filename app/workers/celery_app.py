from celery import Celery
from celery.signals import setup_logging, worker_process_init

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery("vault", broker=settings.broker_url, backend=None, include=["app.workers.tasks"])

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_default_queue="ingest",
    # Two queues: slow LLM analysis must never starve indexing (which makes docs chat-ready).
    task_routes={
        "vault.ingest_document": {"queue": "ingest"},
        "vault.analyze_document": {"queue": "analysis"},
    },
    # Re-deliver a task if the worker dies mid-flight; tasks are idempotent so this is safe.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long tasks: don't hoard messages on one worker
    task_time_limit=15 * 60,
    task_soft_time_limit=14 * 60,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 30 * 60},
)


@setup_logging.connect
def _configure_logging(**_):
    configure_logging(settings.log_level, settings.log_json)


@worker_process_init.connect
def _reset_connections(**_):
    # Forked children must not share the parent's DB connections.
    from app.db import session as db_session

    db_session._sync_engine = None
    db_session._SyncSessionLocal = None
