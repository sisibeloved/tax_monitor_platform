from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache, partial
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import IO, Iterator, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from celery import Celery  # type: ignore[import-untyped]
import pytest
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import Engine, text

from tax_risk.application.refund_writebacks import (
    IncomeTaxRefundWritebackService,
    RefundWritebackDispatchItem,
)
from tax_risk.config import Settings
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.workers.income_tax_refund_writebacks import (
    DELIVER_WRITEBACK_TASK,
    DISPATCH_PENDING_WRITEBACKS_TASK,
    INCOME_TAX_REFUND_WRITEBACK_QUEUE,
    build_refund_writeback_task_kwargs,
    register_income_tax_refund_writeback_tasks,
)


BACKEND_ROOT = Path(__file__).resolve().parents[3]
WORKER_SCOPE_SECRET = "real-refund-worker-integration-scope-secret"
TEST_DATABASE_ENV = "REFUND_REAL_WORKER_DATABASE_URL"
TEST_REDIS_ENV = "REFUND_REAL_WORKER_REDIS_URL"
TEST_TELEMETRY_ENV = "REFUND_REAL_WORKER_TELEMETRY_PREFIX"

pytestmark = pytest.mark.real_redis_worker


class _RedisSender:
    def __init__(self, redis_url: str, telemetry_prefix: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._prefix = telemetry_prefix

    def write_status(self, company_code: str, desired_value: str) -> object:
        del desired_value
        calls_key = f"{self._prefix}:calls:{company_code}"
        call_number = cast(int, self._redis.incr(calls_key))
        mode = self._redis.get(f"{self._prefix}:mode:{company_code}") or "success"
        if mode == "fail-once" and call_number == 1:
            raise RuntimeError("synthetic first delivery failure")
        if mode == "block":
            self._redis.set(f"{self._prefix}:started:{company_code}", "1")
            deadline = time.monotonic() + 60
            while self._redis.get(f"{self._prefix}:release:{company_code}") != "1":
                if time.monotonic() >= deadline:
                    raise RuntimeError("synthetic blocked delivery timed out")
                time.sleep(0.05)
        return object()


class _DispatchOnlySender:
    def write_status(self, company_code: str, desired_value: str) -> object:
        del company_code, desired_value
        raise AssertionError("dispatch scanner must never call the sender")


@lru_cache(maxsize=1)
def _delivery_service_factory() -> IncomeTaxRefundWritebackService:
    database_url = os.environ[TEST_DATABASE_ENV]
    redis_url = os.environ[TEST_REDIS_ENV]
    telemetry_prefix = os.environ[TEST_TELEMETRY_ENV]
    _engine, factory = create_session_factory(database_url)
    return IncomeTaxRefundWritebackService(
        partial(UnitOfWork, factory),
        _RedisSender(redis_url, telemetry_prefix),
        max_retries=1,
    )


@lru_cache(maxsize=1)
def _dispatch_service_factory() -> IncomeTaxRefundWritebackService:
    database_url = os.environ[TEST_DATABASE_ENV]
    _engine, factory = create_session_factory(database_url)
    return IncomeTaxRefundWritebackService(
        partial(UnitOfWork, factory),
        _DispatchOnlySender(),
        max_retries=1,
    )


def _build_worker_app() -> Celery:
    redis_url = os.getenv(TEST_REDIS_ENV, "memory://")
    backend = redis_url if redis_url != "memory://" else "cache+memory://"
    app = Celery(
        "refund-real-worker-integration",
        broker=redis_url,
        backend=backend,
        set_as_current=False,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=("json",),
        enable_utc=True,
        timezone="UTC",
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=30,
        task_soft_time_limit=25,
        result_expires=300,
        broker_transport_options={"visibility_timeout": 2, "polling_interval": 0.1},
        result_backend_transport_options={"visibility_timeout": 2},
        worker_scope_secret=WORKER_SCOPE_SECRET,
        lark_refund_max_retries=1,
        lark_refund_dispatch_batch_size=100,
        quarterly_task_retry_backoff_seconds=1,
        task_routes={
            DELIVER_WRITEBACK_TASK: {"queue": INCOME_TAX_REFUND_WRITEBACK_QUEUE},
            DISPATCH_PENDING_WRITEBACKS_TASK: {"queue": INCOME_TAX_REFUND_WRITEBACK_QUEUE},
        },
    )
    register_income_tax_refund_writeback_tasks(
        app=app,
        service_factory=_delivery_service_factory,
        dispatch_service_factory=_dispatch_service_factory,
    )
    return app


worker_app = _build_worker_app()


@dataclass
class _WorkerHarness:
    app: Celery
    redis: Redis
    redis_url: str
    database_url: str
    telemetry_prefix: str
    hostname: str
    log_path: Path
    process: subprocess.Popen[str] | None = None
    _log_file: IO[str] | None = None

    def start(self) -> None:
        assert self.process is None
        environment = os.environ.copy()
        environment.update(
            {
                TEST_DATABASE_ENV: self.database_url,
                TEST_REDIS_ENV: self.redis_url,
                TEST_TELEMETRY_ENV: self.telemetry_prefix,
                "PYTHONPATH": os.pathsep.join((str(BACKEND_ROOT / "src"), str(BACKEND_ROOT))),
            }
        )
        self._log_file = self.log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                (
                    "tests.integration.workers."
                    "test_income_tax_refund_writeback_real_worker:worker_app"
                ),
                "worker",
                "--pool=solo",
                "--concurrency=1",
                f"--queues={INCOME_TAX_REFUND_WRITEBACK_QUEUE}",
                f"--hostname={self.hostname}",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=BACKEND_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(self._worker_log("worker exited during startup"))
            replies = self.app.control.ping(destination=[self.hostname], timeout=0.5)
            if replies:
                return
            time.sleep(0.2)
        raise AssertionError(self._worker_log("worker did not answer ping"))

    def stop(self, *, abrupt: bool = False) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            if abrupt:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def _worker_log(self, message: str) -> str:
        if self._log_file is not None:
            self._log_file.flush()
        content = self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""
        return f"{message}\n{content[-8_000:]}"


@pytest.fixture
def real_refund_worker(
    isolated_database_url: str,
    tmp_path: Path,
) -> Iterator[_WorkerHarness]:
    redis_url = _test_redis_url(Settings().redis_url)
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    try:
        redis_client.ping()
    except RedisError as error:
        if os.getenv("CI"):
            pytest.fail(f"CI real Redis worker gate cannot reach Redis: {error}")
        pytest.skip("requires a reachable Redis service")
    redis_client.flushdb()
    app = Celery(
        "refund-real-worker-client",
        broker=redis_url,
        backend=redis_url,
        set_as_current=False,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=("json",),
        broker_transport_options={"visibility_timeout": 2, "polling_interval": 0.1},
        result_backend_transport_options={"visibility_timeout": 2},
    )
    harness = _WorkerHarness(
        app=app,
        redis=redis_client,
        redis_url=redis_url,
        database_url=isolated_database_url,
        telemetry_prefix=f"refund-worker-{uuid4().hex}",
        hostname=f"refund-worker-{uuid4().hex}@localhost",
        log_path=tmp_path / "refund-worker.log",
    )
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()
        redis_client.flushdb()
        redis_client.close()


def test_real_redis_worker_delivers_and_replay_is_idempotent(
    real_refund_worker: _WorkerHarness,
) -> None:
    engine, _factory = create_session_factory(real_refund_worker.database_url)
    item, company_code = _seed_writeback(engine, "REAL-SUCCESS")
    try:
        first = _send_delivery(real_refund_worker, item).get(timeout=30)
        replay = _send_delivery(real_refund_worker, item).get(timeout=30)

        assert first["status"] == "SUCCEEDED"
        assert first["claimed"] is True
        assert replay["status"] == "SUCCEEDED"
        assert replay["claimed"] is False
        assert _call_count(real_refund_worker, company_code) == 1
        assert _stored_state(engine, item.writeback_id) == ("SUCCEEDED", 1)
    finally:
        engine.dispose()


def test_real_redis_worker_retries_a_transient_sender_failure(
    real_refund_worker: _WorkerHarness,
) -> None:
    engine, _factory = create_session_factory(real_refund_worker.database_url)
    item, company_code = _seed_writeback(engine, "REAL-RETRY")
    real_refund_worker.redis.set(
        f"{real_refund_worker.telemetry_prefix}:mode:{company_code}",
        "fail-once",
    )
    try:
        result = _send_delivery(real_refund_worker, item).get(timeout=30)

        assert result["status"] == "SUCCEEDED"
        assert result["attempt_count"] == 2
        assert _call_count(real_refund_worker, company_code) == 2
        assert _stored_state(engine, item.writeback_id) == ("SUCCEEDED", 2)
    finally:
        engine.dispose()


def test_periodic_dispatch_task_drains_pending_outbox_through_real_redis(
    real_refund_worker: _WorkerHarness,
) -> None:
    engine, _factory = create_session_factory(real_refund_worker.database_url)
    item, company_code = _seed_writeback(engine, "REAL-DISPATCH")
    try:
        dispatch_result = real_refund_worker.app.signature(
            DISPATCH_PENDING_WRITEBACKS_TASK,
            queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
        ).apply_async()
        dispatched = dispatch_result.get(timeout=30)

        assert dispatched["candidate_count"] >= 1
        assert dispatched["dispatched_count"] >= 1
        _wait_for_state(engine, item.writeback_id, "SUCCEEDED", timeout=30)
        assert _call_count(real_refund_worker, company_code) == 1
    finally:
        engine.dispose()


@pytest.mark.skipif(sys.platform == "win32", reason="abrupt Redis redelivery is gated on Linux CI")
def test_killed_worker_redelivers_processing_task_after_restart(
    real_refund_worker: _WorkerHarness,
) -> None:
    engine, _factory = create_session_factory(real_refund_worker.database_url)
    item, company_code = _seed_writeback(engine, "REAL-KILL")
    prefix = real_refund_worker.telemetry_prefix
    real_refund_worker.redis.set(f"{prefix}:mode:{company_code}", "block")
    result = _send_delivery(real_refund_worker, item)
    try:
        _wait_for_redis_key(
            real_refund_worker.redis,
            f"{prefix}:started:{company_code}",
            timeout=20,
        )
        _wait_for_state(engine, item.writeback_id, "PROCESSING", timeout=20)
        real_refund_worker.stop(abrupt=True)
        real_refund_worker.redis.set(f"{prefix}:mode:{company_code}", "success")
        real_refund_worker.redis.set(f"{prefix}:release:{company_code}", "1")
        time.sleep(3)
        real_refund_worker.start()

        completed = result.get(timeout=45)

        assert completed["status"] == "SUCCEEDED"
        assert completed["attempt_count"] == 2
        assert _call_count(real_refund_worker, company_code) == 2
        assert _stored_state(engine, item.writeback_id) == ("SUCCEEDED", 2)
    finally:
        engine.dispose()


def _send_delivery(harness: _WorkerHarness, item: RefundWritebackDispatchItem):  # type: ignore[no-untyped-def]
    return harness.app.signature(
        DELIVER_WRITEBACK_TASK,
        kwargs=build_refund_writeback_task_kwargs(
            item,
            worker_scope_secret=WORKER_SCOPE_SECRET,
        ),
        queue=INCOME_TAX_REFUND_WRITEBACK_QUEUE,
    ).apply_async()


def _seed_writeback(engine: Engine, suffix: str) -> tuple[RefundWritebackDispatchItem, str]:
    company_id = uuid4()
    target_id = uuid4()
    writeback_id = uuid4()
    company_code = f"REFUND-{suffix}-{uuid4().hex[:6]}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO company (id, company_code, company_name, lifecycle) "
                "VALUES (:id, :code, :name, 'ACTIVE')"
            ),
            {"id": company_id, "code": company_code, "name": company_code},
        )
        connection.execute(
            text(
                "INSERT INTO income_tax_refund_target ("
                "id, company_id, refund_tax_year, source_record_key, expected_amount, "
                "currency, amount_scale, source_version, receipt_status, received_at, "
                "latest_scan_period) VALUES ("
                ":id, :company_id, 2025, :source_key, 100.00, 'CNY', 2, "
                "'worker-v1', 'RECEIVED', now(), '2026-03-31')"
            ),
            {
                "id": target_id,
                "company_id": company_id,
                "source_key": f"worker-target-{target_id}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO income_tax_refund_writeback ("
                "id, target_id, company_id, idempotency_key, desired_value, status, "
                "attempt_count) VALUES ("
                ":id, :target_id, :company_id, :key, '已退税', 'PENDING', 0)"
            ),
            {
                "id": writeback_id,
                "target_id": target_id,
                "company_id": company_id,
                "key": f"worker-writeback:{target_id}",
            },
        )
    return (
        RefundWritebackDispatchItem(
            writeback_id=writeback_id,
            company_id=company_id,
            scope_period=date(2026, 3, 31),
        ),
        company_code,
    )


def _stored_state(engine: Engine, writeback_id: UUID) -> tuple[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, attempt_count FROM income_tax_refund_writeback WHERE id = :id"),
            {"id": writeback_id},
        ).one()
    return str(row.status), int(row.attempt_count)


def _wait_for_state(
    engine: Engine,
    writeback_id: UUID,
    expected: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _stored_state(engine, writeback_id)[0] == expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"writeback {writeback_id} did not reach {expected}")


def _call_count(harness: _WorkerHarness, company_code: str) -> int:
    value = cast(
        str | None,
        harness.redis.get(f"{harness.telemetry_prefix}:calls:{company_code}"),
    )
    return int(value or 0)


def _wait_for_redis_key(client: Redis, key: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(key) is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"Redis key was not created: {key}")


def _test_redis_url(configured_url: str) -> str:
    parsed = urlsplit(configured_url)
    if parsed.scheme not in {"redis", "rediss"}:
        return "redis://127.0.0.1:6379/15"
    return urlunsplit((parsed.scheme, parsed.netloc, "/15", "", ""))
