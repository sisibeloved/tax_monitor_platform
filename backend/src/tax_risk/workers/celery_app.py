from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]

from tax_risk.config import Settings
from tax_risk.workers.quarterly_batch import (
    QUARTERLY_QUEUE,
    RUN_COMPANY_TASK,
    SUMMARIZE_BATCH_TASK,
    default_quarterly_service_factory,
    register_quarterly_tasks,
)


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create the Celery application with durable production worker defaults."""

    resolved = settings or Settings()
    visibility_timeout = resolved.celery_visibility_timeout_seconds
    # Eager work stays in-process, so its stored results should not require Redis.
    result_backend = (
        "cache+memory://" if resolved.celery_task_always_eager else resolved.redis_url
    )
    app = Celery(
        "tax_risk",
        broker=resolved.redis_url,
        backend=result_backend,
    )
    app.conf.update(
        broker_url=resolved.redis_url,
        result_backend=result_backend,
        task_serializer="json",
        result_serializer="json",
        accept_content=("json",),
        enable_utc=True,
        timezone="UTC",
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=resolved.quarterly_task_soft_time_limit_seconds,
        task_time_limit=resolved.quarterly_task_time_limit_seconds,
        worker_concurrency=resolved.quarterly_worker_concurrency,
        task_always_eager=resolved.celery_task_always_eager,
        task_eager_propagates=resolved.celery_task_eager_propagates,
        task_store_eager_result=resolved.celery_task_store_eager_result,
        broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout": visibility_timeout},
        result_backend_transport_options={"visibility_timeout": visibility_timeout},
        visibility_timeout=visibility_timeout,
        result_expires=resolved.celery_result_expires_seconds,
        task_routes={
            RUN_COMPANY_TASK: {"queue": QUARTERLY_QUEUE},
            SUMMARIZE_BATCH_TASK: {"queue": QUARTERLY_QUEUE},
        },
        quarterly_task_max_retries=resolved.quarterly_task_max_retries,
        quarterly_task_retry_backoff_seconds=(
            resolved.quarterly_task_retry_backoff_seconds
        ),
    )
    return app


celery_app = create_celery_app()
register_quarterly_tasks(
    app=celery_app,
    service_factory=default_quarterly_service_factory,
)
app = celery_app

__all__ = ["app", "celery_app", "create_celery_app"]
