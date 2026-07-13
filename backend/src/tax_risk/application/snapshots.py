"""Quality-gated, immutable quarterly accounting snapshots."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
from typing import Any, TypeVar, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tax_risk.adapters.ingest.base import fits_database_amount
from tax_risk.domain.money import Money
from tax_risk.persistence.ingest_models import (
    Company,
    CompanyLifecycle,
    IngestBatch,
    IngestBatchStatus,
    IngestError,
    SourceRecord,
)
from tax_risk.persistence.master_models import TaxMasterVersion, VersionStatus
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
    SnapshotSource,
    SnapshotStatus,
)
from tax_risk.persistence.semantic_models import SapExpenseVoucherSnapshotProjection
from tax_risk.snapshot_limits import (
    MAX_SNAPSHOT_SET_MEMBERS,
    MAX_SNAPSHOT_SOURCE_BATCHES,
)


REQUIRED_QUARTERLY_METRICS: tuple[str, ...] = (
    "cumulative_profit",
    "received_dividends",
    "fair_value_change",
    "cumulative_revenue",
    "prior_quarter_current_tax",
    "current_quarter_current_tax",
    "other_payables_accrual",
    "hesi_no_invoice",
)

UowFactory = Callable[[], UnitOfWork]
BatchRow = TypeVar("BatchRow", SourceRecord, IngestError)


def canonical_sha256(value: object) -> str:
    """Hash canonical UTF-8 JSON without floats or presentation-dependent spacing."""

    payload = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def source_version_set_hash(
    batches: Sequence[Mapping[str, object]],
    master: Mapping[str, object],
) -> str:
    """Hash stable source identities independently from caller batch ordering."""

    ordered = sorted(
        (dict(batch) for batch in batches),
        key=lambda item: str(item.get("id", "")),
    )
    return canonical_sha256({"batches": ordered, "tax_master": dict(master)})


def _canonicalize(value: object) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise TypeError("canonical snapshot JSON does not accept float values")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical snapshot JSON requires finite Decimal values")
        return _decimal_string(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _canonical_utc_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical snapshot JSON requires string mapping keys")
            canonical[key] = _canonicalize(item)
        return canonical
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    raise TypeError(f"unsupported canonical snapshot value: {type(value).__name__}")


def _canonical_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical snapshot JSON requires timezone-aware datetimes")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class QualityIssue:
    category: str
    error_code: str
    source: str
    field: str
    company: str
    period: date
    remediation: str


class SnapshotError(Exception):
    error_code = "SNAPSHOT_ERROR"


class SnapshotRequestError(SnapshotError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SnapshotNotFoundError(SnapshotError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SnapshotConflictError(SnapshotError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SnapshotQualityError(SnapshotError):
    error_code = "SNAPSHOT_QUALITY_FAILED"

    def __init__(self, issues: tuple[QualityIssue, ...]) -> None:
        self.issues = issues
        super().__init__(issues[0].remediation if issues else "snapshot quality gate failed")


@dataclass(frozen=True, slots=True)
class SnapshotView:
    id: UUID
    company_id: UUID
    company_code: str
    tax_master_version_id: UUID
    period: date
    source_version_set_hash: str
    status: SnapshotStatus
    currency: str
    amount_scale: int
    record_count: int
    control_total: Decimal
    checksum: str
    lineage: dict[str, Any]
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class SnapshotValidationResult:
    valid: bool
    issues: tuple[QualityIssue, ...]
    snapshot: SnapshotView | None
    reused: bool = False


@dataclass(frozen=True, slots=True)
class ExpectedSnapshotMember:
    company_id: UUID
    snapshot_id: UUID


@dataclass(frozen=True, slots=True)
class SnapshotSetView:
    id: UUID
    set_key: str
    period: date
    status: SnapshotSetStatus
    expected_member_count: int
    published_at: datetime
    supersedes_snapshot_set_id: UUID | None
    members: tuple[ExpectedSnapshotMember, ...]


@dataclass(frozen=True, slots=True)
class _FrozenSource:
    batch: IngestBatch
    record_count: int
    control_total: Decimal
    lineage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _FrozenSnapshot:
    company: Company
    master: TaxMasterVersion
    sources: tuple[_FrozenSource, ...]
    source_hash: str
    checksum: str
    control_total: Decimal
    lineage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _QualityData:
    company: Company | None
    batches: tuple[IngestBatch, ...]
    records: tuple[SourceRecord, ...]
    errors: tuple[IngestError, ...]
    masters: tuple[TaxMasterVersion, ...]


@dataclass(frozen=True, slots=True)
class _LockedSourceData:
    batches: tuple[IngestBatch, ...]
    records: tuple[SourceRecord, ...]
    errors: tuple[IngestError, ...]


FailureInjector = Callable[[str], None]


class SnapshotService:
    """Application boundary for snapshot validation and atomic publication."""

    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._failure_injector = failure_injector

    def validate(
        self,
        *,
        company_code: str,
        period: date,
        source_batch_ids: Sequence[UUID],
        accepted_partial_batch_ids: Sequence[UUID] = (),
    ) -> SnapshotValidationResult:
        company_code = _required_text(company_code, "company_code", 64)
        _require_quarter_end(period)
        selected_ids, accepted_partial_ids = _validate_source_selection(
            source_batch_ids,
            accepted_partial_batch_ids,
        )

        try:
            with self._uow_factory() as uow:
                probe_company = uow.ingest.get_company_by_code(company_code)
                _lock_named_scopes(
                    uow,
                    (_snapshot_scope(company_code, probe_company, period),),
                )
                existing_snapshots = (
                    tuple(
                        uow.snapshots.list_snapshots_for_company_period(
                            probe_company.id,
                            period,
                            for_update=True,
                        )
                    )
                    if probe_company is not None
                    else ()
                )
                existing_sources = tuple(
                    uow.snapshots.list_sources(
                        (snapshot.id for snapshot in existing_snapshots),
                        for_update=True,
                    )
                )
                source_data = _lock_source_data(uow, selected_ids)
                locked_company = _lock_probed_company(
                    uow,
                    company_code=company_code,
                    probe_company=probe_company,
                )
                data = _complete_quality_data(
                    uow,
                    source_data=source_data,
                    locked_company=locked_company,
                    period=period,
                )
                frozen, issues = _evaluate_quality(
                    company_code=company_code,
                    period=period,
                    selected_ids=selected_ids,
                    accepted_partial_ids=accepted_partial_ids,
                    data=data,
                )
                if frozen is None:
                    return SnapshotValidationResult(False, issues, None)

                existing = next(
                    (
                        snapshot
                        for snapshot in existing_snapshots
                        if snapshot.company_id == frozen.company.id
                        and snapshot.source_version_set_hash == frozen.source_hash
                    ),
                    None,
                )
                if existing is not None:
                    if existing.status != SnapshotStatus.DRAFT:
                        raise SnapshotConflictError(
                            "SNAPSHOT_STATE_CONFLICT",
                            "an immutable snapshot already exists for this source version set",
                        )
                    lineage_issue = _snapshot_lineage_issue(existing, company_code)
                    if lineage_issue is not None:
                        return SnapshotValidationResult(
                            False,
                            (lineage_issue,),
                            None,
                        )
                    matching_sources = tuple(
                        source
                        for source in existing_sources
                        if source.snapshot_id == existing.id
                    )
                    mismatch = _frozen_mismatch(existing, matching_sources, frozen)
                    if mismatch is not None:
                        return SnapshotValidationResult(False, (mismatch,), None)
                    view = _snapshot_view(existing, frozen.company.company_code)
                    return SnapshotValidationResult(True, (), view, reused=True)

                snapshot = AccountingSnapshot(
                    company_id=frozen.company.id,
                    tax_master_version_id=frozen.master.id,
                    period=period,
                    source_version_set_hash=frozen.source_hash,
                    status=SnapshotStatus.DRAFT,
                    currency=frozen.master.currency,
                    amount_scale=frozen.master.amount_scale,
                    record_count=len(REQUIRED_QUARTERLY_METRICS),
                    control_total=frozen.control_total,
                    checksum=frozen.checksum,
                    lineage=deepcopy(frozen.lineage),
                    published_at=None,
                )
                uow.snapshots.add_snapshot(snapshot)
                uow.session.flush()
                self._inject("snapshot_draft_created")
                for expected in frozen.sources:
                    source = _new_snapshot_source(snapshot.id, expected)
                    uow.snapshots.add_source(source)
                uow.session.flush()
                self._inject("snapshot_source_created")
                view = _snapshot_view(snapshot, frozen.company.company_code)
                uow.commit()
                return SnapshotValidationResult(True, (), view)
        except IntegrityError as error:
            raise SnapshotConflictError(
                "SNAPSHOT_CONCURRENT_CONFLICT",
                "snapshot validation conflicted with a concurrent request",
            ) from error

    def publish(self, snapshot_id: UUID) -> SnapshotView:
        try:
            with self._uow_factory() as uow:
                probe = uow.snapshots.get_snapshot(snapshot_id)
                if probe is None:
                    raise SnapshotNotFoundError(
                        "SNAPSHOT_NOT_FOUND",
                        f"snapshot {snapshot_id} was not found",
                    )
                probe_company = uow.ingest.get_company(probe.company_id)
                if probe_company is None:
                    raise SnapshotConflictError(
                        "SNAPSHOT_COMPANY_MISSING",
                        "snapshot references a missing company",
                    )
                _lock_named_scopes(
                    uow,
                    (_snapshot_scope(probe_company.company_code, probe_company, probe.period),),
                )
                snapshot = uow.snapshots.get_snapshot(snapshot_id, for_update=True)
                if snapshot is None:
                    raise SnapshotNotFoundError(
                        "SNAPSHOT_NOT_FOUND",
                        f"snapshot {snapshot_id} was not found",
                    )
                if snapshot.status != SnapshotStatus.DRAFT:
                    raise SnapshotConflictError(
                        "SNAPSHOT_STATE_CONFLICT",
                        f"snapshot cannot be published from {snapshot.status.value}",
                    )
                sources = uow.snapshots.list_sources((snapshot.id,), for_update=True)
                selected_ids = tuple(source.ingest_batch_id for source in sources)
                source_data = _lock_source_data(uow, selected_ids)
                locked_company = _lock_probed_company(
                    uow,
                    company_code=probe_company.company_code,
                    probe_company=probe_company,
                )
                lineage_issue = _snapshot_lineage_issue(
                    snapshot,
                    probe_company.company_code,
                )
                if lineage_issue is not None:
                    raise SnapshotQualityError((lineage_issue,))
                accepted_partial_ids = _accepted_partial_ids(snapshot.lineage)
                data = _complete_quality_data(
                    uow,
                    source_data=source_data,
                    locked_company=locked_company,
                    period=snapshot.period,
                )
                frozen, issues = _evaluate_quality(
                    company_code=probe_company.company_code,
                    period=snapshot.period,
                    selected_ids=selected_ids,
                    accepted_partial_ids=accepted_partial_ids,
                    data=data,
                )
                if frozen is None:
                    raise SnapshotQualityError(issues)
                mismatch = _frozen_mismatch(snapshot, sources, frozen)
                if mismatch is not None:
                    raise SnapshotQualityError((mismatch,))

                snapshot.status = SnapshotStatus.VALIDATED
                uow.session.flush()
                self._inject("snapshot_validated")
                snapshot.status = SnapshotStatus.PUBLISHED
                snapshot.published_at = _database_clock(uow)
                uow.session.flush()
                self._inject("snapshot_published")
                view = _snapshot_view(snapshot, frozen.company.company_code)
                uow.commit()
                return view
        except IntegrityError as error:
            raise SnapshotConflictError(
                "SNAPSHOT_CONCURRENT_CONFLICT",
                "snapshot publication conflicted with a concurrent request",
            ) from error

    def publish_set(
        self,
        *,
        set_key: str,
        period: date,
        expected_members: Sequence[ExpectedSnapshotMember],
        supersedes_snapshot_set_id: UUID | None = None,
    ) -> SnapshotSetView:
        set_key = _required_text(set_key, "set_key", 256)
        _require_quarter_end(period)
        members = _validate_expected_members(expected_members)

        try:
            with self._uow_factory() as uow:
                _lock_named_scopes(uow, (f"snapshot-set:{set_key}",))
                if uow.snapshots.get_snapshot_set_by_key(set_key) is not None:
                    raise SnapshotConflictError(
                        "SNAPSHOT_SET_KEY_CONFLICT",
                        f"snapshot set key {set_key!r} already exists",
                    )

                probe_snapshots = uow.snapshots.list_snapshots(
                    member.snapshot_id for member in members
                )
                if len(probe_snapshots) != len(members):
                    raise SnapshotRequestError(
                        "SNAPSHOT_SET_MEMBER_MISSING",
                        "every expected snapshot must exist",
                    )
                probed_company_rows = uow.ingest.list_companies(
                    snapshot.company_id for snapshot in probe_snapshots
                )
                probe_companies = {
                    company.id: company for company in probed_company_rows
                }
                if len(probe_companies) != len(probe_snapshots):
                    raise SnapshotRequestError(
                        "SNAPSHOT_SET_COMPANY_MISSING",
                        "every expected snapshot company must exist",
                    )
                _lock_named_scopes(
                    uow,
                    (
                        _snapshot_scope(
                            probe_companies[snapshot.company_id].company_code,
                            probe_companies[snapshot.company_id],
                            snapshot.period,
                        )
                        for snapshot in probe_snapshots
                    ),
                )
                snapshots = uow.snapshots.list_snapshots(
                    (member.snapshot_id for member in members),
                    for_update=True,
                )
                sources = uow.snapshots.list_sources(
                    (snapshot.id for snapshot in snapshots),
                    for_update=True,
                )
                _assert_snapshot_member_identity(period, members, snapshots)
                sources_by_snapshot: dict[UUID, list[SnapshotSource]] = defaultdict(list)
                for source in sources:
                    sources_by_snapshot[source.snapshot_id].append(source)
                source_ids_by_snapshot: dict[UUID, tuple[UUID, ...]] = {
                    snapshot.id: tuple(
                        source.ingest_batch_id
                        for source in sources_by_snapshot[snapshot.id]
                    )
                    for snapshot in snapshots
                }
                all_batch_ids = tuple(
                    sorted(
                        {
                            batch_id
                            for batch_ids in source_ids_by_snapshot.values()
                            for batch_id in batch_ids
                        }
                    )
                )
                batches = tuple(uow.ingest.list_batches(all_batch_ids, for_update=True))
                records = tuple(
                    uow.ingest.list_source_records(all_batch_ids, for_update=True)
                )
                errors = tuple(
                    uow.ingest.list_batch_errors(all_batch_ids, for_update=True)
                )
                locked_companies = uow.ingest.lock_companies_shared(
                    {
                        company.company_code
                        for company in probe_companies.values()
                    }
                )
                companies_by_id = {
                    company.id: company
                    for company in locked_companies.values()
                    if company is not None
                }
                if set(companies_by_id) != set(probe_companies):
                    raise SnapshotRequestError(
                        "SNAPSHOT_SET_COMPANY_MISSING",
                        "every expected snapshot company must remain locked and mapped",
                    )
                master_rows = uow.master.published_tax_masters_for_companies(
                    companies_by_id,
                    period,
                    for_update=True,
                )
                masters_by_company: dict[UUID, list[TaxMasterVersion]] = defaultdict(list)
                for master in master_rows:
                    masters_by_company[master.company_id].append(master)
                batches_by_id = {batch.id: batch for batch in batches}
                records_by_batch = _group_rows_by_batch(records)
                errors_by_batch = _group_rows_by_batch(errors)
                for snapshot in snapshots:
                    company = companies_by_id.get(snapshot.company_id)
                    company_code = (
                        company.company_code
                        if company is not None
                        else str(snapshot.company_id)
                    )
                    lineage_issue = _snapshot_lineage_issue(snapshot, company_code)
                    if lineage_issue is not None:
                        raise SnapshotQualityError((lineage_issue,))
                    selected_ids = source_ids_by_snapshot[snapshot.id]
                    quality_data = _QualityData(
                        company=company,
                        batches=tuple(
                            batches_by_id[batch_id]
                            for batch_id in selected_ids
                            if batch_id in batches_by_id
                        ),
                        records=tuple(
                            record
                            for batch_id in selected_ids
                            for record in records_by_batch.get(batch_id, ())
                        ),
                        errors=tuple(
                            error
                            for batch_id in selected_ids
                            for error in errors_by_batch.get(batch_id, ())
                        ),
                        masters=tuple(masters_by_company[snapshot.company_id]),
                    )
                    frozen, issues = _evaluate_quality(
                        company_code=company_code,
                        period=period,
                        selected_ids=selected_ids,
                        accepted_partial_ids=_accepted_partial_ids(snapshot.lineage),
                        data=quality_data,
                    )
                    if frozen is None:
                        raise SnapshotQualityError(issues)
                    mismatch = _frozen_mismatch(
                        snapshot,
                        sources_by_snapshot[snapshot.id],
                        frozen,
                    )
                    if mismatch is not None:
                        raise SnapshotQualityError((mismatch,))

                if supersedes_snapshot_set_id is not None:
                    superseded = uow.snapshots.get_snapshot_set(
                        supersedes_snapshot_set_id,
                        for_update=True,
                    )
                    if (
                        superseded is None
                        or superseded.status != SnapshotSetStatus.PUBLISHED
                        or superseded.period != period
                    ):
                        raise SnapshotRequestError(
                            "INVALID_SUPERSEDES_SNAPSHOT_SET",
                            "supersedes target must be a published snapshot set for the same period",
                        )

                snapshot_set = SnapshotSet(
                    set_key=set_key,
                    period=period,
                    status=SnapshotSetStatus.DRAFT,
                    expected_member_count=len(members),
                    published_at=None,
                    supersedes_snapshot_set_id=supersedes_snapshot_set_id,
                )
                uow.snapshots.add_snapshot_set(snapshot_set)
                uow.session.flush()
                self._inject("snapshot_set_draft_created")
                for expected in members:
                    uow.snapshots.add_member(
                        SnapshotSetMember(
                            snapshot_set_id=snapshot_set.id,
                            company_id=expected.company_id,
                            snapshot_id=expected.snapshot_id,
                        )
                    )
                uow.session.flush()
                self._inject("snapshot_set_member_created")
                for expected in members:
                    # A snapshot that has already appeared in a published set is frozen.
                    # Reusing it must never attach observations ingested later.
                    if uow.semantic.snapshot_is_in_published_set(expected.snapshot_id):
                        continue
                    company = companies_by_id[expected.company_id]
                    projected_ids = uow.semantic.projected_observation_ids(
                        expected.snapshot_id
                    )
                    for observation in uow.semantic.sap_observations_for_company_ytd(
                        company.company_code,
                        period,
                    ):
                        if observation.id in projected_ids:
                            continue
                        uow.semantic.add_sap_projection(
                            SapExpenseVoucherSnapshotProjection(
                                observation_id=observation.id,
                                snapshot_id=expected.snapshot_id,
                                company_code=company.company_code,
                                period=period,
                            )
                        )
                uow.session.flush()
                self._inject("snapshot_set_sap_projected")
                snapshot_set.status = SnapshotSetStatus.VALIDATED
                uow.session.flush()
                self._inject("snapshot_set_validated")
                snapshot_set.status = SnapshotSetStatus.PUBLISHED
                uow.session.flush()
                self._inject("snapshot_set_published")
                if snapshot_set.published_at is None:
                    raise SnapshotConflictError(
                        "SNAPSHOT_SET_TIMESTAMP_MISSING",
                        "database did not assign snapshot set published_at",
                    )
                view = _snapshot_set_view(snapshot_set, members)
                uow.commit()
                return view
        except IntegrityError as error:
            raise SnapshotConflictError(
                "SNAPSHOT_SET_CONCURRENT_CONFLICT",
                "snapshot set publication conflicted with a concurrent request",
            ) from error

    def _inject(self, stage: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(stage)


def _required_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise SnapshotRequestError(
            "INVALID_SNAPSHOT_REQUEST",
            f"{field} must contain between 1 and {maximum} characters",
        )
    return normalized


def _require_quarter_end(period: date) -> None:
    if type(period) is not date:
        raise TypeError("period must be a date")
    quarter_ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    if (period.month, period.day) not in quarter_ends:
        raise SnapshotRequestError(
            "INVALID_QUARTER_PERIOD",
            "period must be a calendar quarter-end date",
        )


def _validate_source_selection(
    source_batch_ids: Sequence[UUID],
    accepted_partial_batch_ids: Sequence[UUID],
) -> tuple[tuple[UUID, ...], frozenset[UUID]]:
    if len(source_batch_ids) > MAX_SNAPSHOT_SOURCE_BATCHES:
        raise SnapshotRequestError(
            "SOURCE_BATCH_LIMIT_EXCEEDED",
            f"source_batch_ids cannot exceed {MAX_SNAPSHOT_SOURCE_BATCHES} items",
        )
    if len(accepted_partial_batch_ids) > MAX_SNAPSHOT_SOURCE_BATCHES:
        raise SnapshotRequestError(
            "PARTIAL_BATCH_LIMIT_EXCEEDED",
            (
                "accepted_partial_batch_ids cannot exceed "
                f"{MAX_SNAPSHOT_SOURCE_BATCHES} items"
            ),
        )
    selected = tuple(source_batch_ids)
    if not selected:
        raise SnapshotRequestError(
            "SOURCE_BATCH_REQUIRED",
            "at least one source batch is required",
        )
    if len(set(selected)) != len(selected):
        raise SnapshotRequestError(
            "DUPLICATE_SOURCE_BATCH",
            "source_batch_ids must be unique",
        )
    accepted = tuple(accepted_partial_batch_ids)
    if len(set(accepted)) != len(accepted):
        raise SnapshotRequestError(
            "DUPLICATE_PARTIAL_ACCEPTANCE",
            "accepted_partial_batch_ids must be unique",
        )
    selected_set = frozenset(selected)
    accepted_set = frozenset(accepted)
    if not accepted_set <= selected_set:
        raise SnapshotRequestError(
            "PARTIAL_BATCH_NOT_SELECTED",
            "accepted partial batches must be a subset of selected batches",
        )
    return tuple(sorted(selected_set)), accepted_set


def _validate_expected_members(
    expected_members: Sequence[ExpectedSnapshotMember],
) -> tuple[ExpectedSnapshotMember, ...]:
    if len(expected_members) > MAX_SNAPSHOT_SET_MEMBERS:
        raise SnapshotRequestError(
            "SNAPSHOT_SET_LIMIT_EXCEEDED",
            f"expected_members cannot exceed {MAX_SNAPSHOT_SET_MEMBERS} items",
        )
    members = tuple(expected_members)
    if len(members) < 100:
        raise SnapshotRequestError(
            "SNAPSHOT_SET_TOO_SMALL",
            "a snapshot set requires at least 100 expected companies",
        )
    company_ids = [member.company_id for member in members]
    snapshot_ids = [member.snapshot_id for member in members]
    if len(set(company_ids)) != len(company_ids):
        raise SnapshotRequestError(
            "DUPLICATE_SNAPSHOT_SET_COMPANY",
            "each expected company must appear exactly once",
        )
    if len(set(snapshot_ids)) != len(snapshot_ids):
        raise SnapshotRequestError(
            "DUPLICATE_SNAPSHOT_SET_SNAPSHOT",
            "each expected snapshot must appear exactly once",
        )
    return tuple(sorted(members, key=lambda item: (item.company_id, item.snapshot_id)))


def _lock_named_scopes(uow: UnitOfWork, scopes: Iterable[str]) -> None:
    for scope in sorted(set(scopes)):
        uow.session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                ":lock_namespace, hashtext(:lock_scope))"
            ),
            {"lock_namespace": 20260713, "lock_scope": scope},
        )


def _snapshot_scope(company_code: str, company: Company | None, period: date) -> str:
    company_identity = str(company.id) if company is not None else f"code:{company_code}"
    return f"snapshot:{company_identity}:{period.isoformat()}"


def _lock_probed_company(
    uow: UnitOfWork,
    *,
    company_code: str,
    probe_company: Company | None,
) -> Company | None:
    locked_company = uow.ingest.lock_companies_shared({company_code})[company_code]
    if (
        locked_company is not None
        and probe_company is not None
        and locked_company.id != probe_company.id
    ):
        return None
    return locked_company


def _lock_source_data(
    uow: UnitOfWork,
    batch_ids: Sequence[UUID],
) -> _LockedSourceData:
    batches = tuple(uow.ingest.list_batches(batch_ids, for_update=True))
    records = tuple(uow.ingest.list_source_records(batch_ids, for_update=True))
    errors = tuple(uow.ingest.list_batch_errors(batch_ids, for_update=True))
    return _LockedSourceData(
        batches=batches,
        records=records,
        errors=errors,
    )


def _complete_quality_data(
    uow: UnitOfWork,
    *,
    source_data: _LockedSourceData,
    locked_company: Company | None,
    period: date,
) -> _QualityData:
    masters = (
        tuple(
            uow.master.published_tax_masters(
                locked_company.id,
                period,
                for_update=True,
            )
        )
        if locked_company is not None
        else ()
    )
    return _QualityData(
        company=locked_company,
        batches=source_data.batches,
        records=source_data.records,
        errors=source_data.errors,
        masters=masters,
    )


def _group_rows_by_batch(
    rows: Iterable[BatchRow],
) -> dict[UUID, tuple[BatchRow, ...]]:
    grouped: dict[UUID, list[BatchRow]] = defaultdict(list)
    for row in rows:
        grouped[row.batch_id].append(row)
    return {batch_id: tuple(items) for batch_id, items in grouped.items()}


def _evaluate_quality(
    *,
    company_code: str,
    period: date,
    selected_ids: Sequence[UUID],
    accepted_partial_ids: frozenset[UUID],
    data: _QualityData,
) -> tuple[_FrozenSnapshot | None, tuple[QualityIssue, ...]]:
    issues: list[QualityIssue] = []
    company = data.company
    if company is None or company.lifecycle != CompanyLifecycle.ACTIVE:
        issues.append(
            _issue(
                "COMPANY_UNMAPPED",
                "company_master",
                "company_code",
                company_code,
                period,
                "Map and activate the company in the controlled company master.",
            )
        )

    batches_by_id = {batch.id: batch for batch in data.batches}
    records_by_batch: dict[UUID, list[SourceRecord]] = defaultdict(list)
    for record in data.records:
        records_by_batch[record.batch_id].append(record)
    errors_by_batch: dict[UUID, list[IngestError]] = defaultdict(list)
    for error in data.errors:
        errors_by_batch[error.batch_id].append(error)

    ordered_batches: list[IngestBatch] = []
    partial_decisions: dict[UUID, dict[str, Any]] = {}
    batch_target_required: dict[UUID, list[SourceRecord]] = defaultdict(list)
    metric_records: dict[str, list[SourceRecord]] = defaultdict(list)
    common_currency: str | None = None
    common_scale: int | None = None

    for batch_id in sorted(selected_ids):
        batch = batches_by_id.get(batch_id)
        if batch is None:
            issues.append(
                _issue(
                    "SOURCE_NOT_READY",
                    "ingest_batch",
                    "source_batch_ids",
                    company_code,
                    period,
                    f"Load source batch {batch_id} successfully before validation.",
                )
            )
            continue
        ordered_batches.append(batch)
        batch_records = records_by_batch.get(batch.id, [])
        if batch.dataset_code != "quarterly_metric":
            issues.append(
                _issue(
                    "SOURCE_NOT_READY",
                    batch.source,
                    "dataset_code",
                    company_code,
                    period,
                    "Select only controlled quarterly_metric batches.",
                )
            )
        decision, rejected_error_details = _partial_decision(
            batch,
            errors_by_batch.get(batch.id, []),
            company_code=company_code,
            accepted=batch.id in accepted_partial_ids,
        )
        partial_decisions[batch.id] = decision
        if rejected_error_details:
            issues.append(
                _noncanonical_json_issue(
                    source=batch.source,
                    field="ingest_error.details",
                    company=company_code,
                    period=period,
                )
            )
        if batch.rejected_count != len(errors_by_batch.get(batch.id, [])):
            issues.append(
                _issue(
                    "CONTROL_TOTAL_MISMATCH",
                    batch.source,
                    "rejected_count",
                    company_code,
                    period,
                    "Reconcile rejected_count to the complete persisted ingest-error evidence.",
                )
            )
        if batch.status == IngestBatchStatus.PARTIAL:
            if not decision["accepted"] or not decision["safe"]:
                issues.append(
                    _issue(
                        "SOURCE_NOT_READY",
                        batch.source,
                        "status",
                        company_code,
                        period,
                        "Resolve related/unknown rejected rows or explicitly accept only a proven unrelated partial batch.",
                    )
                )
        elif batch.status != IngestBatchStatus.SUCCEEDED:
            issues.append(
                _issue(
                    "SOURCE_NOT_READY",
                    batch.source,
                    "status",
                    company_code,
                    period,
                    "Complete source ingestion successfully before snapshot validation.",
                )
            )
        elif batch.rejected_count or errors_by_batch.get(batch.id):
            issues.append(
                _issue(
                    "SOURCE_NOT_READY",
                    batch.source,
                    "status",
                    company_code,
                    period,
                    "A SUCCEEDED batch must have no rejected rows or ingest errors; correct its terminal status.",
                )
            )
        elif batch.id in accepted_partial_ids:
            issues.append(
                _issue(
                    "SOURCE_NOT_READY",
                    batch.source,
                    "accepted_partial_batch_ids",
                    company_code,
                    period,
                    "Remove non-PARTIAL batches from accepted_partial_batch_ids.",
                )
            )

        if batch.period != period:
            issues.append(
                _issue(
                    "SOURCE_METADATA_MISMATCH",
                    batch.source,
                    "period",
                    company_code,
                    period,
                    "Select a batch for the requested quarter-end period.",
                )
            )
        if common_currency is None:
            common_currency = batch.currency
            common_scale = batch.amount_scale
        elif batch.currency != common_currency or batch.amount_scale != common_scale:
            issues.append(
                _issue(
                    "SOURCE_METADATA_MISMATCH",
                    batch.source,
                    "currency_amount_scale",
                    company_code,
                    period,
                    "Use batches with one currency and amount scale.",
                )
            )
        if batch.record_count != batch.accepted_count + batch.rejected_count:
            issues.append(
                _issue(
                    "CONTROL_TOTAL_MISMATCH",
                    batch.source,
                    "record_count",
                    company_code,
                    period,
                    "Reconcile batch accepted and rejected record counts.",
                )
            )
        if batch.accepted_count != len(batch_records):
            issues.append(
                _issue(
                    "CONTROL_TOTAL_MISMATCH",
                    batch.source,
                    "accepted_count",
                    company_code,
                    period,
                    "Rebuild the batch so accepted_count equals persisted source rows.",
                )
            )
        batch_total = _money_sum(
            (cast(Decimal, record.amount) for record in batch_records),
            batch.currency,
            batch.amount_scale,
        )
        if batch_total != batch.control_total:
            issues.append(
                _issue(
                    "CONTROL_TOTAL_MISMATCH",
                    batch.source,
                    "control_total",
                    company_code,
                    period,
                    "Reconcile the exact Decimal source-row total to the batch control total.",
                )
            )

        for record in batch_records:
            payload_is_canonical_object = _is_canonical_json(record.payload)
            if not payload_is_canonical_object:
                issues.append(
                    _noncanonical_json_issue(
                        source=batch.source,
                        field="source_record.payload",
                        company=company_code,
                        period=period,
                    )
                )
            if not _is_canonical_json(record.lineage):
                issues.append(
                    _noncanonical_json_issue(
                        source=batch.source,
                        field="source_record.lineage",
                        company=company_code,
                        period=period,
                    )
                )
            row_mismatch = _source_record_mismatch(record, batch)
            if row_mismatch is not None:
                issues.append(
                    _issue(
                        "SOURCE_METADATA_MISMATCH",
                        batch.source,
                        row_mismatch,
                        company_code,
                        period,
                        "Correct source-row metadata and re-ingest the batch.",
                    )
                )
            if company is None or record.company_id != company.id:
                continue
            if not payload_is_canonical_object:
                continue
            payload_company = record.payload.get("company_code")
            if payload_company is not None and payload_company != company_code:
                issues.append(
                    _issue(
                        "SOURCE_METADATA_MISMATCH",
                        batch.source,
                        "company_code",
                        company_code,
                        period,
                        "Re-ingest the row with company payload matching its controlled company id.",
                    )
                )
            payload_mismatch = _source_payload_mismatch(record, batch)
            if payload_mismatch is not None:
                issues.append(
                    _issue(
                        "SOURCE_METADATA_MISMATCH",
                        batch.source,
                        payload_mismatch,
                        company_code,
                        period,
                        "Re-ingest the row so canonical payload values equal persisted typed columns.",
                    )
                )
            metric_code = record.payload.get("metric_code")
            if isinstance(metric_code, str) and metric_code in REQUIRED_QUARTERLY_METRICS:
                batch_target_required[batch.id].append(record)
                metric_records[metric_code].append(record)
        if not batch_target_required[batch.id]:
            issues.append(
                _issue(
                    "SOURCE_NOT_READY",
                    batch.source,
                    "company_contribution",
                    company_code,
                    period,
                    "Select only batches contributing a required metric for this company.",
                )
            )

    for metric_code in REQUIRED_QUARTERLY_METRICS:
        matches = metric_records.get(metric_code, [])
        if not matches:
            issues.append(
                _issue(
                    "MISSING_REQUIRED_METRIC",
                    "quarterly_metric",
                    metric_code,
                    company_code,
                    period,
                    f"Provide exactly one {metric_code} source row; missing values are never treated as zero.",
                )
            )
        elif len(matches) > 1:
            issues.append(
                _issue(
                    "DUPLICATE_SOURCE_ROW",
                    "quarterly_metric",
                    metric_code,
                    company_code,
                    period,
                    f"Retain exactly one controlled {metric_code} row for the company and period.",
                )
            )

    masters = tuple(data.masters)
    if not masters:
        issues.append(
            _issue(
                "TAX_MASTER_MISSING",
                "tax_master",
                "effective_version",
                company_code,
                period,
                "Publish one tax master version effective on the quarter end.",
            )
        )
    elif len(masters) > 1:
        issues.append(
            _issue(
                "TAX_MASTER_DUPLICATE",
                "tax_master",
                "effective_version",
                company_code,
                period,
                "Retire overlapping tax master versions so exactly one is effective.",
            )
        )
    else:
        master = masters[0]
        if master.status != VersionStatus.PUBLISHED:
            issues.append(
                _issue(
                    "TAX_MASTER_MISSING",
                    "tax_master",
                    "status",
                    company_code,
                    period,
                    "Approve the effective tax master before snapshot validation.",
                )
            )
        if (
            common_currency is not None
            and (master.currency != common_currency or master.amount_scale != common_scale)
        ):
            issues.append(
                _issue(
                    "TAX_MASTER_METADATA_MISMATCH",
                    "tax_master",
                    "currency_amount_scale",
                    company_code,
                    period,
                    "Align tax master currency and amount scale with quarterly sources.",
                )
            )

    if issues:
        return None, _sorted_issues(issues)
    assert company is not None
    assert len(masters) == 1
    assert common_currency is not None and common_scale is not None
    ordered_metrics = tuple(metric_records[metric][0] for metric in REQUIRED_QUARTERLY_METRICS)
    control_total = _money_sum(
        (cast(Decimal, record.amount) for record in ordered_metrics),
        common_currency,
        common_scale,
    )
    if not fits_database_amount(control_total):
        overflow = _issue(
            "CONTROL_TOTAL_MISMATCH",
            "quarterly_metric",
            "snapshot_control_total",
            company_code,
            period,
            "Correct source amounts whose required-metric total exceeds NUMERIC(38,12).",
        )
        return None, (overflow,)
    return (
        _freeze_snapshot(
            company=company,
            period=period,
            batches=tuple(ordered_batches),
            records=ordered_metrics,
            records_by_batch=batch_target_required,
            partial_decisions=partial_decisions,
            master=masters[0],
            control_total=control_total,
        ),
        (),
    )


def _partial_decision(
    batch: IngestBatch,
    errors: Sequence[IngestError],
    *,
    company_code: str,
    accepted: bool,
) -> tuple[dict[str, Any], bool]:
    evidence: list[dict[str, Any]] = []
    safe = True
    rejected_details = False
    for error in sorted(errors, key=lambda item: (item.row_number, item.id)):
        try:
            if not isinstance(error.details, Mapping):
                raise TypeError("ingest_error.details must be a JSON object")
            canonical_details = _canonicalize(error.details)
        except (TypeError, ValueError):
            safe = False
            rejected_details = True
            evidence.append(
                {
                    "id": str(error.id),
                    "row_number": error.row_number,
                    "error_code": error.error_code,
                    "classification": "NON_CANONICAL_JSON",
                    "details_rejected": True,
                }
            )
            continue
        error_company = error.details.get("company_code")
        metric_code = error.details.get("metric_code")
        if isinstance(error_company, str) and error_company != company_code:
            classification = "OTHER_COMPANY"
        elif (
            error_company == company_code
            and isinstance(metric_code, str)
            and metric_code not in REQUIRED_QUARTERLY_METRICS
        ):
            classification = "NON_REQUIRED_METRIC"
        elif error_company == company_code:
            classification = "RELATED_ERROR"
            safe = False
        else:
            classification = "UNKNOWN_OWNERSHIP"
            safe = False
        evidence.append(
            {
                "id": str(error.id),
                "row_number": error.row_number,
                "error_code": error.error_code,
                "classification": classification,
                "details": canonical_details,
            }
        )
    if batch.status == IngestBatchStatus.PARTIAL and not errors:
        safe = False
        evidence.append(
            {
                "id": None,
                "row_number": None,
                "error_code": "MISSING_PARTIAL_EVIDENCE",
                "classification": "UNKNOWN_OWNERSHIP",
                "details": {},
            }
        )
    return (
        {
            "accepted": accepted,
            "safe": safe,
            "policy": (
                "all rejected rows must identify another company or the target company with a non-required metric"
            ),
            "evidence": evidence,
        },
        rejected_details,
    )


def _is_canonical_json(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        _canonicalize(value)
    except (TypeError, ValueError):
        return False
    return True


def _snapshot_lineage_issue(
    snapshot: AccountingSnapshot,
    company_code: str,
) -> QualityIssue | None:
    if _is_canonical_json(snapshot.lineage):
        return None
    return _noncanonical_json_issue(
        source="accounting_snapshot",
        field="accounting_snapshot.lineage",
        company=company_code,
        period=snapshot.period,
    )


def _noncanonical_json_issue(
    *,
    source: str,
    field: str,
    company: str,
    period: date,
) -> QualityIssue:
    return _issue(
        "NON_CANONICAL_JSON",
        source,
        field,
        company,
        period,
        (
            "Use a JSON object, encode exact numeric values as Decimal strings, "
            "and remove unsupported values."
        ),
    )


def _source_record_mismatch(record: SourceRecord, batch: IngestBatch) -> str | None:
    if record.dataset_code != batch.dataset_code:
        return "dataset_code"
    if record.period != batch.period:
        return "period"
    if record.currency != batch.currency:
        return "currency"
    if record.amount_scale != batch.amount_scale:
        return "amount_scale"
    return None


def _source_payload_mismatch(record: SourceRecord, batch: IngestBatch) -> str | None:
    expected: tuple[tuple[str, object], ...] = (
        ("period", record.period.isoformat()),
        ("currency", record.currency),
        ("amount_scale", record.amount_scale),
    )
    for field, value in expected:
        if record.payload.get(field) != value:
            return f"payload.{field}"
    payload_amount = record.payload.get("amount")
    if not isinstance(payload_amount, str):
        return "payload.amount"
    try:
        parsed_amount = Decimal(payload_amount)
    except InvalidOperation:
        return "payload.amount"
    if not parsed_amount.is_finite() or parsed_amount != record.amount:
        return "payload.amount"
    payload_metric = record.payload.get("metric_code")
    if not isinstance(payload_metric, str) or not payload_metric.strip():
        return "payload.metric_code"
    payload_dataset = record.payload.get("dataset_code")
    if payload_dataset is not None and payload_dataset != batch.dataset_code:
        return "payload.dataset_code"
    payload_key = record.payload.get("source_record_key")
    if payload_key is not None and payload_key != record.source_record_key:
        return "payload.source_record_key"
    return None


def _freeze_snapshot(
    *,
    company: Company,
    period: date,
    batches: tuple[IngestBatch, ...],
    records: tuple[SourceRecord, ...],
    records_by_batch: Mapping[UUID, Sequence[SourceRecord]],
    partial_decisions: Mapping[UUID, dict[str, Any]],
    master: TaxMasterVersion,
    control_total: Decimal,
) -> _FrozenSnapshot:
    master_lineage = _master_lineage(master)
    frozen_sources: list[_FrozenSource] = []
    batch_identities: list[dict[str, Any]] = []
    for batch in sorted(batches, key=lambda item: item.id):
        subset = sorted(
            records_by_batch.get(batch.id, ()),
            key=lambda item: (
                REQUIRED_QUARTERLY_METRICS.index(str(item.payload.get("metric_code"))),
                item.source_record_key,
                item.id,
            ),
        )
        subset_total = _money_sum(
            (cast(Decimal, record.amount) for record in subset),
            batch.currency,
            batch.amount_scale,
        )
        batch_lineage = {
            "batch": {
                "id": str(batch.id),
                "source": batch.source,
                "source_batch_key": batch.source_batch_key,
                "dataset_code": batch.dataset_code,
                "status": batch.status.value,
                "period": batch.period.isoformat(),
                "schema_version": batch.schema_version,
                "currency": batch.currency,
                "amount_scale": batch.amount_scale,
                "record_count": batch.record_count,
                "accepted_count": batch.accepted_count,
                "rejected_count": batch.rejected_count,
                "control_total": _decimal_string(batch.control_total),
                "checksum": batch.checksum,
                "extraction_time": _canonical_utc_timestamp(batch.extraction_time),
                "payload_ref": batch.payload_ref,
            },
            "target_subset": {
                "company_id": str(company.id),
                "company_code": company.company_code,
                "record_count": len(subset),
                "control_total": _decimal_string(subset_total),
                "metric_codes": [record.payload["metric_code"] for record in subset],
                "source_record_ids": [str(record.id) for record in subset],
            },
            "partial_decision": deepcopy(partial_decisions[batch.id]),
        }
        frozen_sources.append(
            _FrozenSource(
                batch=batch,
                record_count=len(subset),
                control_total=subset_total,
                lineage=batch_lineage,
            )
        )
        batch_identities.append(
            {
                "id": str(batch.id),
                "source": batch.source,
                "source_batch_key": batch.source_batch_key,
                "checksum": batch.checksum,
                "schema_version": batch.schema_version,
                "extraction_time": _canonical_utc_timestamp(batch.extraction_time),
                "payload_ref": batch.payload_ref,
                "partial_decision": deepcopy(partial_decisions[batch.id]),
                "target_subset_checksum": canonical_sha256(
                    [
                        {
                            "metric_code": record.payload["metric_code"],
                            "amount": record.amount,
                            "source_record_id": record.id,
                            "source_record_key": record.source_record_key,
                            "payload": record.payload,
                            "lineage": record.lineage,
                        }
                        for record in subset
                    ]
                ),
            }
        )
    metric_lineage = []
    for metric_code, record in zip(REQUIRED_QUARTERLY_METRICS, records, strict=True):
        metric_lineage.append(
            {
                "metric_code": metric_code,
                "amount": _decimal_string(cast(Decimal, record.amount)),
                "source_record": {
                    "id": str(record.id),
                    "batch_id": str(record.batch_id),
                    "source_record_key": record.source_record_key,
                    "payload": _canonicalize(record.payload),
                    "lineage": _canonicalize(record.lineage),
                },
            }
        )
    lineage: dict[str, Any] = {
        "schema_version": "quarterly-accounting-snapshot-v2",
        "company": {
            "id": str(company.id),
            "company_code": company.company_code,
            "company_name": company.company_name,
            "lifecycle": company.lifecycle.value,
        },
        "period": period.isoformat(),
        "currency": master.currency,
        "amount_scale": master.amount_scale,
        "metrics": metric_lineage,
        "sources": [source.lineage for source in frozen_sources],
        "tax_master": master_lineage,
    }
    master_identity = {
        "id": master_lineage["id"],
        "version": master_lineage["version"],
        "checksum": master_lineage["source_checksum"],
        "valid_from": master_lineage["valid_from"],
        "valid_to": master_lineage["valid_to"],
        "tax_rate": master_lineage["tax_rate"],
        "loss_carryforward": master_lineage["loss_carryforward"],
        "three_year_average_tax_burden": master_lineage[
            "three_year_average_tax_burden"
        ],
        "currency": master_lineage["currency"],
        "amount_scale": master_lineage["amount_scale"],
        "source_file_name": master_lineage["source_file_name"],
        "imported_at": master_lineage["imported_at"],
    }
    return _FrozenSnapshot(
        company=company,
        master=master,
        sources=tuple(frozen_sources),
        source_hash=source_version_set_hash(batch_identities, master_identity),
        checksum=canonical_sha256(lineage),
        control_total=control_total,
        lineage=lineage,
    )


def _master_lineage(master: TaxMasterVersion) -> dict[str, Any]:
    return {
        "id": str(master.id),
        "version": master.version,
        "source_batch_id": str(master.source_batch_id),
        "source_checksum": master.source_checksum,
        "source_row_number": master.source_row_number,
        "source_file_name": master.source_file_name,
        "imported_at": _canonical_utc_timestamp(master.created_at),
        "valid_from": master.valid_from.isoformat(),
        "valid_to": master.valid_to.isoformat() if master.valid_to is not None else None,
        "tax_rate": _decimal_string(master.tax_rate),
        "loss_carryforward": _decimal_string(master.loss_carryforward),
        "three_year_average_tax_burden": _decimal_string(
            master.average_tax_burden_rate_3y
        ),
        "currency": master.currency,
        "amount_scale": master.amount_scale,
    }


def _money_sum(amounts: Iterable[Decimal], currency: str, scale: int) -> Decimal:
    total = Money.unrounded("0", currency=currency, scale=scale)
    for amount in amounts:
        total = total + Money.unrounded(amount, currency=currency, scale=scale)
    return total.amount


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _new_snapshot_source(snapshot_id: UUID, expected: _FrozenSource) -> SnapshotSource:
    return SnapshotSource(
        snapshot_id=snapshot_id,
        ingest_batch_id=expected.batch.id,
        source=expected.batch.source,
        source_version=expected.batch.schema_version,
        record_count=expected.record_count,
        control_total=expected.control_total,
        currency=expected.batch.currency,
        amount_scale=expected.batch.amount_scale,
        lineage=deepcopy(expected.lineage),
    )


def _frozen_mismatch(
    snapshot: AccountingSnapshot,
    sources: Sequence[SnapshotSource],
    frozen: _FrozenSnapshot,
) -> QualityIssue | None:
    scalar_matches = (
        snapshot.company_id == frozen.company.id
        and snapshot.tax_master_version_id == frozen.master.id
        and snapshot.source_version_set_hash == frozen.source_hash
        and snapshot.currency == frozen.master.currency
        and snapshot.amount_scale == frozen.master.amount_scale
        and snapshot.record_count == len(REQUIRED_QUARTERLY_METRICS)
        and snapshot.control_total == frozen.control_total
        and snapshot.checksum == frozen.checksum
        and snapshot.lineage == frozen.lineage
    )
    expected_by_batch = {source.batch.id: source for source in frozen.sources}
    actual_by_batch = {source.ingest_batch_id: source for source in sources}
    source_matches = set(expected_by_batch) == set(actual_by_batch)
    if source_matches:
        for batch_id, expected in expected_by_batch.items():
            actual = actual_by_batch[batch_id]
            if not (
                actual.source == expected.batch.source
                and actual.source_version == expected.batch.schema_version
                and actual.record_count == expected.record_count
                and actual.control_total == expected.control_total
                and actual.currency == expected.batch.currency
                and actual.amount_scale == expected.batch.amount_scale
                and actual.lineage == expected.lineage
            ):
                source_matches = False
                break
    if scalar_matches and source_matches:
        return None
    return _issue(
        "FROZEN_SNAPSHOT_MISMATCH",
        "accounting_snapshot",
        "checksum",
        frozen.company.company_code,
        snapshot.period,
        "Create a new snapshot from corrected source/master versions; never mutate frozen lineage.",
    )


def _accepted_partial_ids(lineage: object) -> frozenset[UUID]:
    if not isinstance(lineage, Mapping):
        return frozenset()
    accepted: set[UUID] = set()
    sources = lineage.get("sources")
    if not isinstance(sources, list):
        return frozenset()
    for source in sources:
        if not isinstance(source, dict):
            continue
        batch = source.get("batch")
        decision = source.get("partial_decision")
        if (
            isinstance(batch, dict)
            and isinstance(batch.get("id"), str)
            and isinstance(decision, dict)
            and decision.get("accepted") is True
        ):
            try:
                accepted.add(UUID(batch["id"]))
            except (ValueError, TypeError):
                continue
    return frozenset(accepted)


def _assert_snapshot_member_identity(
    period: date,
    expected_members: Sequence[ExpectedSnapshotMember],
    snapshots: Sequence[AccountingSnapshot],
) -> None:
    snapshots_by_id = {snapshot.id: snapshot for snapshot in snapshots}
    if len(snapshots_by_id) != len(expected_members):
        raise SnapshotRequestError(
            "SNAPSHOT_SET_MEMBER_MISSING",
            "every expected snapshot must exist and be locked",
        )
    for expected in expected_members:
        snapshot = snapshots_by_id.get(expected.snapshot_id)
        if snapshot is None:
            raise SnapshotRequestError(
                "SNAPSHOT_SET_MEMBER_MISSING",
                f"snapshot {expected.snapshot_id} is missing",
            )
        if snapshot.company_id != expected.company_id:
            raise SnapshotRequestError(
                "SNAPSHOT_SET_IDENTITY_MISMATCH",
                "expected company and snapshot identities must match exactly",
            )
        if snapshot.period != period:
            raise SnapshotRequestError(
                "SNAPSHOT_SET_PERIOD_MISMATCH",
                "all snapshots must use the set quarter-end period",
            )
        if snapshot.status != SnapshotStatus.PUBLISHED:
            raise SnapshotRequestError(
                "SNAPSHOT_SET_MEMBER_NOT_PUBLISHED",
                "all expected snapshots must be published",
            )


def _database_clock(uow: UnitOfWork) -> datetime:
    current = uow.session.scalar(select(func.clock_timestamp()))
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise SnapshotConflictError(
            "DATABASE_CLOCK_INVALID",
            "database clock did not return a timezone-aware timestamp",
        )
    return current.astimezone(timezone.utc)


def _snapshot_view(snapshot: AccountingSnapshot, company_code: str) -> SnapshotView:
    return SnapshotView(
        id=snapshot.id,
        company_id=snapshot.company_id,
        company_code=company_code,
        tax_master_version_id=snapshot.tax_master_version_id,
        period=snapshot.period,
        source_version_set_hash=snapshot.source_version_set_hash,
        status=snapshot.status,
        currency=snapshot.currency,
        amount_scale=snapshot.amount_scale,
        record_count=snapshot.record_count,
        control_total=snapshot.control_total,
        checksum=snapshot.checksum,
        lineage=deepcopy(snapshot.lineage),
        published_at=snapshot.published_at,
    )


def _snapshot_set_view(
    snapshot_set: SnapshotSet,
    members: Sequence[ExpectedSnapshotMember],
) -> SnapshotSetView:
    assert snapshot_set.published_at is not None
    published_at = snapshot_set.published_at
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return SnapshotSetView(
        id=snapshot_set.id,
        set_key=snapshot_set.set_key,
        period=snapshot_set.period,
        status=snapshot_set.status,
        expected_member_count=snapshot_set.expected_member_count,
        published_at=published_at.astimezone(timezone.utc),
        supersedes_snapshot_set_id=snapshot_set.supersedes_snapshot_set_id,
        members=tuple(members),
    )


def _issue(
    error_code: str,
    source: str,
    field: str,
    company: str,
    period: date,
    remediation: str,
) -> QualityIssue:
    return QualityIssue(
        category="DATA_QUALITY",
        error_code=error_code,
        source=source,
        field=field,
        company=company,
        period=period,
        remediation=remediation,
    )


def _sorted_issues(issues: Sequence[QualityIssue]) -> tuple[QualityIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda item: (
                item.error_code,
                item.source,
                item.field,
                item.company,
                item.period,
                item.remediation,
            ),
        )
    )


__all__ = [
    "ExpectedSnapshotMember",
    "QualityIssue",
    "REQUIRED_QUARTERLY_METRICS",
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotNotFoundError",
    "SnapshotQualityError",
    "SnapshotRequestError",
    "SnapshotService",
    "SnapshotSetView",
    "SnapshotValidationResult",
    "SnapshotView",
    "canonical_sha256",
    "source_version_set_hash",
]
