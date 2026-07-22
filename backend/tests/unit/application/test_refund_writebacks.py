from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from types import TracebackType
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement

from tax_risk.application.refund_writebacks import IncomeTaxRefundWritebackService
from tax_risk.persistence.income_tax_refund_models import IncomeTaxRefundWriteback


class _Result:
    def __init__(
        self,
        *,
        row: object | None = None,
        scalar: object | None = None,
        rows: Sequence[object] = (),
    ) -> None:
        self._row = row
        self._scalar = scalar
        self._rows = tuple(rows)

    def one_or_none(self) -> object | None:
        return self._row

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def all(self) -> Sequence[object]:
        return self._rows


class _Session:
    def __init__(
        self,
        result: _Result,
        *,
        observed: IncomeTaxRefundWriteback | None = None,
    ) -> None:
        self._result = result
        self._observed = observed
        self.statements: list[object] = []

    def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return self._result

    def get(
        self,
        model: type[IncomeTaxRefundWriteback],
        identity: UUID,
    ) -> IncomeTaxRefundWriteback | None:
        del model, identity
        return self._observed


class _Uow:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.commits = 0

    def __enter__(self) -> _Uow:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def commit(self) -> None:
        self.commits += 1


class _UowFactory:
    def __init__(self, *uows: _Uow) -> None:
        self._uows = iter(uows)

    def __call__(self) -> _Uow:
        return next(self._uows)


class _Sender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def write_status(self, company_code: str, desired_value: str) -> object:
        self.calls.append((company_code, desired_value))
        if self.error is not None:
            raise self.error
        return object()


class _SchemaAwareSender(_Sender):
    def __init__(self, preflight_error: Exception | None = None) -> None:
        super().__init__()
        self.preflight_error = preflight_error
        self.preflight_calls = 0

    def ensure_schema(self) -> object:
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error
        return object()


class _PermanentSenderError(RuntimeError):
    error_code = "LARK_REFUND_RECORD_NOT_FOUND"
    retryable = False


class _UnsafeCodeError(RuntimeError):
    error_code = "AppSecret=must-not-be-stored"
    retryable = False


class _RateLimitError(RuntimeError):
    error_code = "LARK_REFUND_RATE_LIMITED"

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


def _compile_postgresql(statement: object) -> str:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(
        cast(ClauseElement, statement).compile(
            dialect=dialect,
            compile_kwargs={"literal_binds": True},
        )
    )


def test_negative_retry_configuration_is_rejected_before_database_access() -> None:
    with pytest.raises(ValueError, match="max_retries must be nonnegative"):
        IncomeTaxRefundWritebackService(
            _UowFactory(),  # type: ignore[arg-type]
            _Sender(),
            max_retries=-1,
        )


def _writeback(*, status: str = "PENDING", attempt_count: int = 0) -> IncomeTaxRefundWriteback:
    return IncomeTaxRefundWriteback(
        id=uuid4(),
        target_id=uuid4(),
        company_id=uuid4(),
        idempotency_key=f"refund-received:{uuid4()}",
        desired_value="已退税",
        status=status,
        attempt_count=attempt_count,
        last_error=(
            "REFUND_WRITEBACK_DELIVERY_FAILED:RuntimeError" if status == "FAILED" else None
        ),
        processed_at=(datetime.now(timezone.utc) if status == "SUCCEEDED" else None),
    )


def test_delivery_claims_then_marks_the_row_succeeded() -> None:
    writeback = _writeback()
    claim_session = _Session(_Result(row=(writeback, "3000")))
    finish_session = _Session(_Result(scalar=writeback))
    claim_uow = _Uow(claim_session)
    finish_uow = _Uow(finish_session)
    sender = _Sender()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(claim_uow, finish_uow),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id, expected_company_id=writeback.company_id)

    assert sender.calls == [("3000", "已退税")]
    assert outcome.status == "SUCCEEDED"
    assert outcome.attempt_count == 1
    assert outcome.claimed is True
    assert outcome.retryable is False
    assert writeback.status == "SUCCEEDED"
    assert writeback.last_error is None
    assert writeback.processed_at is not None
    assert claim_uow.commits == finish_uow.commits == 1
    compiled = _compile_postgresql(claim_session.statements[0])
    assert "FOR UPDATE OF income_tax_refund_writeback SKIP LOCKED" in compiled


def test_delivery_runs_schema_preflight_before_the_external_write() -> None:
    writeback = _writeback()
    claim_uow = _Uow(_Session(_Result(row=(writeback, "3000"))))
    finish_uow = _Uow(_Session(_Result(scalar=writeback)))
    sender = _SchemaAwareSender()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(claim_uow, finish_uow),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id, expected_company_id=writeback.company_id)

    assert sender.preflight_calls == 1
    assert sender.calls == [("3000", "已退税")]
    assert outcome.status == "SUCCEEDED"


def test_failure_is_bounded_redacted_and_stops_after_configured_retries() -> None:
    writeback = _writeback()
    sender = _Sender(RuntimeError("AppSecret=do-not-persist-me"))
    first_claim = _Uow(_Session(_Result(row=(writeback, "3000"))))
    first_finish = _Uow(_Session(_Result(scalar=writeback)))
    second_claim = _Uow(_Session(_Result(row=(writeback, "3000"))))
    second_finish = _Uow(_Session(_Result(scalar=writeback)))
    service = IncomeTaxRefundWritebackService(
        _UowFactory(first_claim, first_finish, second_claim, second_finish),  # type: ignore[arg-type]
        sender,
        max_retries=1,
    )

    first = service.deliver(writeback.id)
    second = service.deliver(writeback.id)

    assert first.status == second.status == "FAILED"
    assert first.retryable is True
    assert second.retryable is False
    assert second.attempt_count == 2
    assert writeback.status == "FAILED"
    assert writeback.processed_at is None
    assert writeback.last_error == "REFUND_WRITEBACK_DELIVERY_FAILED:RuntimeError"
    assert "do-not-persist-me" not in writeback.last_error
    assert sender.calls == [("3000", "已退税"), ("3000", "已退税")]


def test_processing_row_is_reclaimed_when_the_broker_redelivers_its_task() -> None:
    writeback = _writeback(status="PROCESSING", attempt_count=4)
    claim_uow = _Uow(_Session(_Result(row=(writeback, "3000"))))
    finish_uow = _Uow(_Session(_Result(scalar=writeback)))
    sender = _Sender()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(claim_uow, finish_uow),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id)

    assert outcome.status == "SUCCEEDED"
    assert outcome.attempt_count == 5
    assert outcome.claimed is True
    assert outcome.retryable is False
    assert sender.calls == [("3000", "已退税")]
    assert claim_uow.commits == finish_uow.commits == 1
    compiled = _compile_postgresql(claim_uow.session.statements[0])
    assert "income_tax_refund_writeback.status = 'PROCESSING'" in compiled
    assert (
        "income_tax_refund_writeback.status = 'FAILED') OR "
        "income_tax_refund_writeback.status = 'PROCESSING'"
    ) in compiled


def test_declared_permanent_adapter_error_is_preserved_without_retry() -> None:
    writeback = _writeback()
    sender = _Sender(_PermanentSenderError("credential-bearing detail is ignored"))
    service = IncomeTaxRefundWritebackService(
        _UowFactory(
            _Uow(_Session(_Result(row=(writeback, "3000")))),
            _Uow(_Session(_Result(scalar=writeback))),
        ),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id)

    assert outcome.status == "FAILED"
    assert outcome.retryable is False
    assert outcome.error_code == "LARK_REFUND_RECORD_NOT_FOUND"
    assert writeback.last_error == "LARK_REFUND_RECORD_NOT_FOUND"


def test_unsafe_declared_error_code_falls_back_to_a_type_only_label() -> None:
    writeback = _writeback()
    sender = _Sender(_UnsafeCodeError("tenant token and secret must stay out of storage"))
    service = IncomeTaxRefundWritebackService(
        _UowFactory(
            _Uow(_Session(_Result(row=(writeback, "3000")))),
            _Uow(_Session(_Result(scalar=writeback))),
        ),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id)

    assert outcome.retryable is False
    assert outcome.error_code == "REFUND_WRITEBACK_DELIVERY_FAILED:_UnsafeCodeError"
    assert writeback.last_error == outcome.error_code
    assert "AppSecret" not in writeback.last_error
    assert "tenant token" not in writeback.last_error


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        pytest.param(12.2, 13, id="fraction-rounds-up"),
        pytest.param(99_999.0, 3_600, id="untrusted-hint-is-clamped"),
        pytest.param(float("nan"), None, id="nonfinite-hint-is-ignored"),
        pytest.param(-1.0, None, id="negative-hint-is-ignored"),
    ],
)
def test_rate_limit_retry_hint_is_safely_carried_in_the_delivery_outcome(
    hint: float,
    expected: int | None,
) -> None:
    writeback = _writeback()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(
            _Uow(_Session(_Result(row=(writeback, "3000")))),
            _Uow(_Session(_Result(scalar=writeback))),
        ),  # type: ignore[arg-type]
        _Sender(_RateLimitError(hint)),
        max_retries=3,
    )

    outcome = service.deliver(writeback.id)

    assert outcome.status == "FAILED"
    assert outcome.retryable is True
    assert outcome.error_code == "LARK_REFUND_RATE_LIMITED"
    assert outcome.retry_after_seconds == expected
    assert outcome.to_payload()["retry_after_seconds"] == expected


def test_missing_writeback_is_not_claimed_or_delivered() -> None:
    uow = _Uow(_Session(_Result(), observed=None))
    sender = _Sender()
    writeback_id = uuid4()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(uow),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback_id)

    assert outcome.writeback_id == writeback_id
    assert outcome.status == "NOT_FOUND"
    assert outcome.claimed is False
    assert outcome.error_code == "WRITEBACK_NOT_FOUND"
    assert sender.calls == []
    assert uow.commits == 0


def test_already_succeeded_writeback_is_an_idempotent_noop() -> None:
    writeback = _writeback(status="SUCCEEDED", attempt_count=1)
    uow = _Uow(_Session(_Result(), observed=writeback))
    sender = _Sender()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(uow),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id)

    assert outcome.status == "SUCCEEDED"
    assert outcome.attempt_count == 1
    assert outcome.claimed is False
    assert outcome.retryable is False
    assert sender.calls == []
    assert uow.commits == 0


def test_failed_writeback_past_retry_limit_is_not_claimed() -> None:
    writeback = _writeback(status="FAILED", attempt_count=2)
    uow = _Uow(_Session(_Result(), observed=writeback))
    sender = _Sender()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(uow),  # type: ignore[arg-type]
        sender,
        max_retries=1,
    )

    outcome = service.deliver(writeback.id)

    assert outcome.status == "FAILED"
    assert outcome.attempt_count == 2
    assert outcome.claimed is False
    assert outcome.retryable is False
    assert sender.calls == []
    assert uow.commits == 0


def test_finish_generation_race_does_not_overwrite_the_newer_claim() -> None:
    writeback = _writeback()
    claim_uow = _Uow(_Session(_Result(row=(writeback, "3000"))))
    stale_finish_uow = _Uow(_Session(_Result(scalar=None)))
    sender = _Sender()
    service = IncomeTaxRefundWritebackService(
        _UowFactory(claim_uow, stale_finish_uow),  # type: ignore[arg-type]
        sender,
        max_retries=3,
    )

    outcome = service.deliver(writeback.id)

    assert sender.calls == [("3000", "已退税")]
    assert outcome.status == "PROCESSING"
    assert outcome.claimed is True
    assert outcome.retryable is False
    assert outcome.error_code == "WRITEBACK_STATE_CHANGED"
    assert claim_uow.commits == 1
    assert stale_finish_uow.commits == 0


def test_expected_company_mismatch_is_reported_as_not_found() -> None:
    writeback = _writeback()
    uow = _Uow(_Session(_Result(), observed=writeback))
    service = IncomeTaxRefundWritebackService(
        _UowFactory(uow),  # type: ignore[arg-type]
        _Sender(),
        max_retries=3,
    )

    outcome = service.deliver(writeback.id, expected_company_id=uuid4())

    assert outcome.status == "NOT_FOUND"
    assert outcome.company_id is None
    assert outcome.error_code == "WRITEBACK_NOT_FOUND"


def test_list_dispatchable_projects_signed_scope_and_supports_id_filtering() -> None:
    writeback_id = uuid4()
    company_id = uuid4()
    scope_period = date(2026, 6, 30)
    omitted_id = uuid4()
    first_session = _Session(
        _Result(
            rows=(
                (writeback_id, company_id, scope_period),
                (omitted_id, company_id, None),
            )
        )
    )
    second_session = _Session(_Result(rows=((writeback_id, company_id, scope_period),)))
    service = IncomeTaxRefundWritebackService(
        _UowFactory(_Uow(first_session), _Uow(second_session)),  # type: ignore[arg-type]
        _Sender(),
        max_retries=3,
    )

    items = service.list_dispatchable(
        limit=7,
        writeback_ids=(writeback_id, writeback_id),
    )
    ids = service.list_dispatchable_ids(limit=10, writeback_ids=(writeback_id,))

    assert len(items) == 1
    assert items[0].writeback_id == writeback_id
    assert items[0].company_id == company_id
    assert items[0].scope_period == scope_period
    assert ids == (writeback_id,)
    compiled = _compile_postgresql(first_session.statements[0])
    assert "income_tax_refund_writeback.attempt_count <= 3" in compiled
    assert "income_tax_refund_writeback.status = 'PENDING'" in compiled
    assert "income_tax_refund_writeback.status = 'FAILED'" in compiled
    assert "income_tax_refund_writeback.status = 'PROCESSING'" not in compiled
    assert compiled.count(str(writeback_id)) == 1
    assert "LIMIT 7" in compiled


def test_empty_writeback_id_filter_short_circuits_without_database_access() -> None:
    service = IncomeTaxRefundWritebackService(
        _UowFactory(),  # type: ignore[arg-type]
        _Sender(),
        max_retries=3,
    )

    assert service.list_dispatchable(writeback_ids=()) == ()
    assert service.list_dispatchable_ids(writeback_ids=()) == ()


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_dispatch_limit_is_rejected_without_database_access(limit: int) -> None:
    service = IncomeTaxRefundWritebackService(
        _UowFactory(),  # type: ignore[arg-type]
        _Sender(),
        max_retries=3,
    )

    try:
        service.list_dispatchable(limit=limit)
    except ValueError as error:
        assert str(error) == "limit must be positive"
    else:
        raise AssertionError("expected ValueError")
