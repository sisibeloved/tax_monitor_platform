from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]

from tax_risk.config import Settings
from tax_risk.observability.context import install_celery_context
from tax_risk.observability.tracing import configure_structured_logging
from tax_risk.workers.business_entertainment import (
    BUSINESS_ENTERTAINMENT_QUEUE,
    RUN_COMPANY_TASK as RUN_BUSINESS_ENTERTAINMENT_COMPANY_TASK,
    default_business_entertainment_service_factory,
    register_business_entertainment_tasks,
)
from tax_risk.workers.quarterly_batch import (
    QUARTERLY_QUEUE,
    RUN_COMPANY_TASK,
    SUMMARIZE_BATCH_TASK,
    default_quarterly_service_factory,
    register_quarterly_tasks,
)
from tax_risk.workers.monthly_semantic import (
    MONTHLY_SEMANTIC_QUEUE,
    RUN_COMPANY_TASK as RUN_MONTHLY_SEMANTIC_COMPANY_TASK,
    SUMMARIZE_TASK as SUMMARIZE_MONTHLY_SEMANTIC_TASK,
    default_monthly_service_factory,
    register_monthly_tasks,
)
from tax_risk.workers.exports import (
    EXPORT_QUEUE,
    RENDER_EXPORT_TASK,
    default_export_service_factory,
    register_export_tasks,
)


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create the Celery application with durable production worker defaults."""

    resolved = settings or Settings()
    if resolved.environment == "production":
        configure_structured_logging()
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
            RUN_BUSINESS_ENTERTAINMENT_COMPANY_TASK: {
                "queue": BUSINESS_ENTERTAINMENT_QUEUE
            },
            RUN_MONTHLY_SEMANTIC_COMPANY_TASK: {"queue": MONTHLY_SEMANTIC_QUEUE},
            SUMMARIZE_MONTHLY_SEMANTIC_TASK: {"queue": MONTHLY_SEMANTIC_QUEUE},
            RENDER_EXPORT_TASK: {"queue": EXPORT_QUEUE},
        },
        quarterly_task_max_retries=resolved.quarterly_task_max_retries,
        quarterly_task_retry_backoff_seconds=(
            resolved.quarterly_task_retry_backoff_seconds
        ),
    )
    install_celery_context(app)
    return app


celery_app = create_celery_app()
register_quarterly_tasks(
    app=celery_app,
    service_factory=default_quarterly_service_factory,
)
register_business_entertainment_tasks(
    app=celery_app,
    service_factory=default_business_entertainment_service_factory,
)
register_monthly_tasks(
    app=celery_app,
    service_factory=default_monthly_service_factory,
)
register_export_tasks(app=celery_app, service_factory=default_export_service_factory)
app = celery_app

__all__ = ["app", "celery_app", "create_celery_app"]
