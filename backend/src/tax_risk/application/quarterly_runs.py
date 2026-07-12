"""Atomic, idempotent persistence for one quarterly company calculation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import MAX_EMAX, MIN_EMIN, ROUND_HALF_UP, Context, Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text

from tax_risk.domain.cases import case_fingerprint
from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import (
    CalculationStatus as DomainCalculationStatus,
    QuarterlyInputs,
    QuarterlyResult,
    calculate_quarterly,
)
from tax_risk.persistence.ingest_models import Company, CompanyLifecycle
from tax_risk.persistence.master_models import RuleVersion, TaxMasterVersion, VersionStatus
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import (
    CalculationStatus,
    DetectionRecord,
    MonitorType,
    MonitoringRun,
    MonitoringRunStatus,
    MonitoringRunType,
    RiskCase,
    RiskCaseStatus,
)
from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
    SnapshotStatus,
)


UowFactory = Callable[[], UnitOfWork]
FailureInjector = Callable[[str], None]
MONITOR_ORDER: tuple[MonitorType, ...] = (
    MonitorType.ACCRUAL_ACCURACY,
    MonitorType.TAX_BURDEN,
    MonitorType.POTENTIAL_TAX_COST,
)
APPROVED_QUARTERLY_MANIFEST: dict[str, object] = {
    "schema_version": "QUARTERLY_V1",
    "rounding_mode": "ROUND_HALF_UP",
    "formulas": {
        "base_before_floor": (
            "cumulative_profit-received_dividends-fair_value_change-loss_carryforward"
        ),
        "cumulative_base": "max(base_before_floor,0)",
        "cumulative_tax_payable": "cumulative_base*tax_rate",
        "current_quarter_should_accrue": ("cumulative_tax_payable-prior_quarter_current_tax"),
        "current_quarter_difference": ("current_quarter_should_accrue-current_quarter_current_tax"),
        "current_tax_burden": "cumulative_tax_payable/cumulative_revenue",
        "tax_burden_deviation": ("current_tax_burden-historical_average_tax_burden"),
        "potential_adjustment": "other_payables_accrual+hesi_no_invoice",
        "potential_base": "max(base_before_floor+potential_adjustment,0)",
        "potential_tax_payable": "potential_base*tax_rate",
        "potential_tax_cost": "potential_tax_payable-cumulative_tax_payable",
    },
    "alert_boundaries": {
        "accrual_accuracy": "difference != 0",
        "tax_burden": "deviation >= 0.05 or deviation <= -0.05",
        "potential_tax_cost": "cost != 0",
    },
}
APPROVED_QUARTERLY_MANIFEST_SHA256 = sha256(
    json.dumps(
        APPROVED_QUARTERLY_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


class QuarterlyRunError(Exception):
    """A stable application error for an invalid frozen-run input."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class QuarterlyRunResult:
    run_id: UUID
    snapshot_id: UUID
    detection_ids: tuple[UUID, ...]
    case_ids: tuple[UUID, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _FrozenMaster:
    tax_rate: Decimal
    loss_carryforward: Decimal
    average_tax_burden_rate_3y: Decimal


@dataclass(frozen=True, slots=True)
class _ReplayContext:
    run: MonitoringRun
    snapshot_set: SnapshotSet
    snapshot: AccountingSnapshot


@dataclass(frozen=True, slots=True)
class _RunContext:
    run: MonitoringRun
    snapshot_set: SnapshotSet
    snapshot: AccountingSnapshot
    company: Company
    master: TaxMasterVersion
    frozen_master: _FrozenMaster
    rule: RuleVersion


@dataclass(frozen=True, slots=True)
class _DetectionValues:
    monitor_type: MonitorType
    calculation_status: DomainCalculationStatus
    input_amount: Decimal | None
    result_amount: Decimal | None
    difference_amount: Decimal | None
    tax_burden_rate: Decimal | None
    tax_burden_deviation: Decimal | None
    reason: str | None
    alert_code: str | None
    direction: str | None


class QuarterlyRunService:
    """Calculate and persist one member of a frozen quarterly monitoring run."""

    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._failure_injector = failure_injector

    def execute(self, *, run_id: UUID, snapshot_id: UUID) -> QuarterlyRunResult:
        if not isinstance(run_id, UUID) or not isinstance(snapshot_id, UUID):
            raise QuarterlyRunError(
                "INVALID_QUARTERLY_RUN_REQUEST",
                "run_id and snapshot_id must be UUID values",
            )

        with self._uow_factory() as uow:
            _lock_scopes(uow, (f"quarterly-run:{run_id}:{snapshot_id}",))
            existing = _existing_detections(uow, run_id, snapshot_id)
            if existing:
                replay_context = _load_replay_context(
                    uow,
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                )
                company_code = _assert_complete_retry(existing, replay_context)
                _assert_replayable_run_status(replay_context.run)
                replay_case_ids = _case_ids_for_detections(
                    uow,
                    replay_context.run,
                    company_code,
                    existing,
                )
                return QuarterlyRunResult(
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    detection_ids=tuple(
                        _by_monitor(existing)[monitor].id for monitor in MONITOR_ORDER
                    ),
                    case_ids=replay_case_ids,
                    replayed=True,
                )
            context = _load_context(uow, run_id=run_id, snapshot_id=snapshot_id)
            _assert_first_execution_status(context.run)

            inputs = _quarterly_inputs(context.snapshot, context.frozen_master)
            calculation = calculate_quarterly(inputs)
            formula_substitution = _canonical_json(calculation.formula_substitution)
            lineage = _detection_lineage(context)
            values = _detection_values(calculation, inputs)

            detections: list[DetectionRecord] = []
            for monitor_values in values:
                detection = _new_detection(
                    context=context,
                    values=monitor_values,
                    calculation=calculation,
                    formula_substitution=formula_substitution,
                    lineage=lineage,
                )
                uow.risks.add_detection(detection)
                uow.session.flush()
                detections.append(detection)
                self._inject("detection_persisted")

            alert_detections = tuple(
                detection for detection in detections if detection.alert_code is not None
            )
            fingerprints = tuple(
                case_fingerprint(
                    context.company.company_code,
                    context.run.fiscal_year,
                    context.run.quarter,
                    detection.monitor_type.value,
                )
                for detection in alert_detections
            )
            _lock_scopes(uow, (f"risk-case:{value}" for value in fingerprints))

            persisted_case_ids: list[UUID] = []
            for fingerprint, detection in zip(
                fingerprints,
                alert_detections,
                strict=True,
            ):
                risk_case = uow.risks.get_case_by_fingerprint(fingerprint)
                if risk_case is None:
                    risk_case = _new_case(
                        context=context,
                        detection=detection,
                        fingerprint=fingerprint,
                    )
                    uow.risks.add_case(risk_case)
                elif _is_newer_case_detection(uow, context.run, risk_case):
                    _refresh_case_summary(risk_case, detection)
                uow.session.flush()
                persisted_case_ids.append(risk_case.id)
                self._inject("case_persisted")

            uow.commit()
            return QuarterlyRunResult(
                run_id=run_id,
                snapshot_id=snapshot_id,
                detection_ids=tuple(detection.id for detection in detections),
                case_ids=tuple(persisted_case_ids),
                replayed=False,
            )

    def run_company(self, *, run_id: UUID, snapshot_id: UUID) -> QuarterlyRunResult:
        """Compatibility spelling for batch orchestration callers."""

        return self.execute(run_id=run_id, snapshot_id=snapshot_id)

    def _inject(self, stage: str) -> None:
        if self._failure_injector is not None:
            self._failure_injector(stage)


def _lock_scopes(uow: UnitOfWork, scopes: Iterable[str]) -> None:
    for scope in sorted(set(scopes)):
        uow.session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:scope))"),
            {"namespace": 20260714, "scope": scope},
        )


def _load_replay_context(
    uow: UnitOfWork,
    *,
    run_id: UUID,
    snapshot_id: UUID,
) -> _ReplayContext:
    run = uow.session.scalar(
        select(MonitoringRun)
        .where(MonitoringRun.id == run_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise QuarterlyRunError("MONITORING_RUN_NOT_FOUND", f"run {run_id} was not found")
    if run.run_type != MonitoringRunType.QUARTERLY:
        raise QuarterlyRunError(
            "MONITORING_RUN_TYPE_INVALID",
            "only QUARTERLY monitoring runs can execute the quarterly calculator",
        )
    snapshot_set = uow.session.scalar(
        select(SnapshotSet).where(SnapshotSet.id == run.snapshot_set_id).with_for_update(read=True)
    )
    if snapshot_set is None or snapshot_set.status != SnapshotSetStatus.PUBLISHED:
        raise QuarterlyRunError(
            "SNAPSHOT_SET_NOT_PUBLISHED",
            "the run must reference one published snapshot set",
        )

    snapshot = uow.session.scalar(
        select(AccountingSnapshot)
        .where(AccountingSnapshot.id == snapshot_id)
        .with_for_update(read=True)
    )
    if snapshot is None or snapshot.status != SnapshotStatus.PUBLISHED:
        raise QuarterlyRunError(
            "SNAPSHOT_NOT_PUBLISHED",
            "the requested accounting snapshot must be published",
        )
    member = uow.session.scalar(
        select(SnapshotSetMember).where(
            SnapshotSetMember.snapshot_set_id == snapshot_set.id,
            SnapshotSetMember.snapshot_id == snapshot.id,
            SnapshotSetMember.company_id == snapshot.company_id,
        )
    )
    if member is None:
        raise QuarterlyRunError(
            "SNAPSHOT_NOT_IN_RUN_SET",
            "the requested snapshot is not a member of the run's frozen snapshot set",
        )
    if snapshot.period != snapshot_set.period:
        raise QuarterlyRunError(
            "SNAPSHOT_SET_PERIOD_MISMATCH",
            "snapshot and frozen snapshot set periods differ",
        )
    quarter = (snapshot.period.month - 1) // 3 + 1
    if snapshot.period.year != run.fiscal_year or quarter != run.quarter:
        raise QuarterlyRunError(
            "MONITORING_RUN_PERIOD_MISMATCH",
            "run fiscal year and quarter must match the frozen snapshot period",
        )

    return _ReplayContext(
        run=run,
        snapshot_set=snapshot_set,
        snapshot=snapshot,
    )


def _load_context(
    uow: UnitOfWork,
    *,
    run_id: UUID,
    snapshot_id: UUID,
) -> _RunContext:
    replay = _load_replay_context(
        uow,
        run_id=run_id,
        snapshot_id=snapshot_id,
    )
    run = replay.run
    snapshot_set = replay.snapshot_set
    snapshot = replay.snapshot

    company = uow.session.scalar(
        select(Company).where(Company.id == snapshot.company_id).with_for_update(read=True)
    )
    if company is None or company.lifecycle != CompanyLifecycle.ACTIVE:
        raise QuarterlyRunError(
            "COMPANY_NOT_CONTROLLED",
            "the snapshot company must remain active and controlled",
        )

    master = uow.session.scalar(
        select(TaxMasterVersion)
        .where(TaxMasterVersion.id == snapshot.tax_master_version_id)
        .with_for_update(read=True)
    )
    if (
        master is None
        or master.company_id != company.id
        or master.status != VersionStatus.PUBLISHED
        or master.valid_from > snapshot.period
        or (master.valid_to is not None and master.valid_to < snapshot.period)
    ):
        raise QuarterlyRunError(
            "TAX_MASTER_NOT_EFFECTIVE",
            "the snapshot must reference its effective published tax master",
        )
    if master.currency != snapshot.currency or master.amount_scale != snapshot.amount_scale:
        raise QuarterlyRunError(
            "TAX_MASTER_METADATA_MISMATCH",
            "snapshot and frozen tax master currency/scale differ",
        )
    frozen_master = _frozen_master(snapshot, master)

    rule = uow.session.scalar(
        select(RuleVersion).where(RuleVersion.id == run.rule_version_id).with_for_update(read=True)
    )
    if (
        rule is None
        or rule.rule_code != "QUARTERLY_V1"
        or rule.status != VersionStatus.PUBLISHED
        or rule.effective_from > snapshot.period
        or (rule.effective_to is not None and rule.effective_to < snapshot.period)
    ):
        raise QuarterlyRunError(
            "QUARTERLY_RULE_NOT_EFFECTIVE",
            "the run must pin an effective published QUARTERLY_V1 rule version",
        )
    assert_approved_quarterly_rule_manifest(rule)

    return _RunContext(
        run=run,
        snapshot_set=snapshot_set,
        snapshot=snapshot,
        company=company,
        master=master,
        frozen_master=frozen_master,
        rule=rule,
    )


def assert_approved_quarterly_rule_manifest(rule: RuleVersion) -> None:
    """Reject any quarterly rule outside the fixed reviewed manifest."""

    definition = rule.definition
    manifest = definition.get("formula_manifest")
    stored_hash = definition.get("formula_manifest_sha256")
    if not isinstance(manifest, dict) or not isinstance(stored_hash, str):
        raise QuarterlyRunError(
            "QUARTERLY_RULE_MANIFEST_INVALID",
            "the pinned quarterly rule lacks its reviewed formula manifest",
        )
    try:
        canonical = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise QuarterlyRunError(
            "QUARTERLY_RULE_MANIFEST_INVALID",
            "the pinned quarterly formula manifest is not canonical JSON",
        ) from error
    recomputed_hash = sha256(canonical).hexdigest()
    if (
        definition.get("review_status") != "REVIEWED"
        or recomputed_hash != stored_hash
        or stored_hash != APPROVED_QUARTERLY_MANIFEST_SHA256
        or manifest != APPROVED_QUARTERLY_MANIFEST
    ):
        raise QuarterlyRunError(
            "QUARTERLY_RULE_MANIFEST_INVALID",
            "the pinned rule does not match the fixed reviewed QUARTERLY_V1 manifest",
        )


def _frozen_master(
    snapshot: AccountingSnapshot,
    current: TaxMasterVersion,
) -> _FrozenMaster:
    lineage = snapshot.lineage.get("tax_master")
    if not isinstance(lineage, dict):
        raise QuarterlyRunError(
            "FROZEN_MASTER_MISMATCH",
            "snapshot lineage lacks its frozen tax-master evidence",
        )
    try:
        tax_rate = _lineage_decimal(lineage, "tax_rate")
        loss_carryforward = _lineage_decimal(lineage, "loss_carryforward")
        average_tax_burden = _lineage_decimal(
            lineage,
            "three_year_average_tax_burden",
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise QuarterlyRunError(
            "FROZEN_MASTER_MISMATCH",
            "snapshot tax-master lineage contains invalid exact values",
        ) from error

    expected = {
        "id": str(current.id),
        "version": current.version,
        "source_batch_id": str(current.source_batch_id),
        "source_checksum": current.source_checksum,
        "source_row_number": current.source_row_number,
        "valid_from": current.valid_from.isoformat(),
        "valid_to": current.valid_to.isoformat() if current.valid_to is not None else None,
        "currency": current.currency,
        "amount_scale": current.amount_scale,
    }
    if (
        any(lineage.get(field) != value for field, value in expected.items())
        or tax_rate != current.tax_rate
        or loss_carryforward != current.loss_carryforward
        or average_tax_burden != current.average_tax_burden_rate_3y
    ):
        raise QuarterlyRunError(
            "FROZEN_MASTER_MISMATCH",
            "current tax master no longer matches the snapshot's frozen lineage",
        )
    return _FrozenMaster(
        tax_rate=tax_rate,
        loss_carryforward=loss_carryforward,
        average_tax_burden_rate_3y=average_tax_burden,
    )


def _lineage_decimal(lineage: dict[str, Any], field: str) -> Decimal:
    raw = lineage.get(field)
    if not isinstance(raw, str):
        raise TypeError(f"{field} must be an exact decimal string")
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _quarterly_inputs(
    snapshot: AccountingSnapshot,
    master: _FrozenMaster,
) -> QuarterlyInputs:
    raw_metrics = snapshot.lineage.get("metrics")
    if not isinstance(raw_metrics, list):
        raise QuarterlyRunError(
            "SNAPSHOT_METRICS_INVALID",
            "frozen snapshot lineage must contain a metric list",
        )
    metrics: dict[str, Decimal] = {}
    for raw_metric in raw_metrics:
        if not isinstance(raw_metric, dict):
            raise QuarterlyRunError(
                "SNAPSHOT_METRICS_INVALID",
                "every frozen snapshot metric must be an object",
            )
        metric_code = raw_metric.get("metric_code")
        raw_amount = raw_metric.get("amount")
        if not isinstance(metric_code, str) or not isinstance(raw_amount, str):
            raise QuarterlyRunError(
                "SNAPSHOT_METRICS_INVALID",
                "frozen metric codes and exact amounts must be strings",
            )
        if metric_code in metrics:
            raise QuarterlyRunError(
                "SNAPSHOT_METRICS_DUPLICATE",
                f"snapshot metric {metric_code!r} appears more than once",
            )
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation as error:
            raise QuarterlyRunError(
                "SNAPSHOT_METRICS_INVALID",
                f"snapshot metric {metric_code!r} is not an exact decimal",
            ) from error
        if not amount.is_finite():
            raise QuarterlyRunError(
                "SNAPSHOT_METRICS_INVALID",
                f"snapshot metric {metric_code!r} must be finite",
            )
        metrics[metric_code] = amount

    required = {
        "cumulative_profit",
        "received_dividends",
        "fair_value_change",
        "cumulative_revenue",
        "prior_quarter_current_tax",
        "current_quarter_current_tax",
        "other_payables_accrual",
        "hesi_no_invoice",
    }
    missing = sorted(required - metrics.keys())
    if missing:
        raise QuarterlyRunError(
            "SNAPSHOT_METRICS_MISSING",
            f"frozen snapshot is missing quarterly metrics: {', '.join(missing)}",
        )

    def money(metric_code: str) -> Money:
        return Money.unrounded(
            metrics[metric_code],
            currency=snapshot.currency,
            scale=snapshot.amount_scale,
        )

    return QuarterlyInputs(
        cumulative_profit=money("cumulative_profit"),
        received_dividends=money("received_dividends"),
        fair_value_change=money("fair_value_change"),
        loss_carryforward=Money.unrounded(
            master.loss_carryforward,
            currency=snapshot.currency,
            scale=snapshot.amount_scale,
        ),
        tax_rate=Rate.from_fraction(master.tax_rate),
        prior_quarter_current_tax=money("prior_quarter_current_tax"),
        current_quarter_current_tax=money("current_quarter_current_tax"),
        cumulative_revenue=money("cumulative_revenue"),
        historical_average_tax_burden=Rate.from_fraction(master.average_tax_burden_rate_3y),
        other_payables_accrual=money("other_payables_accrual"),
        hesi_no_invoice=money("hesi_no_invoice"),
    )


def _detection_values(
    calculation: QuarterlyResult,
    inputs: QuarterlyInputs,
) -> tuple[_DetectionValues, ...]:
    burden_status = calculation.tax_burden_status
    burden_reason = calculation.tax_burden_not_calculated_reason
    burden_alert_code = calculation.tax_burden_alert_code
    burden_direction = _burden_direction(burden_alert_code)
    try:
        burden_rate = _database_decimal(calculation.current_tax_burden)
        burden_deviation = _database_decimal(calculation.tax_burden_deviation)
    except QuarterlyRunError as error:
        if error.error_code != "RATE_VALUE_OVERFLOW":
            raise
        burden_status = DomainCalculationStatus.FAILED
        burden_reason = "RATE_VALUE_OVERFLOW"
        burden_alert_code = None
        burden_direction = None
        burden_rate = None
        burden_deviation = None

    return (
        _DetectionValues(
            monitor_type=MonitorType.ACCRUAL_ACCURACY,
            calculation_status=calculation.accrual_status,
            input_amount=inputs.current_quarter_current_tax.amount,
            result_amount=_amount(calculation.current_quarter_should_accrue),
            difference_amount=_amount(calculation.current_quarter_difference),
            tax_burden_rate=None,
            tax_burden_deviation=None,
            reason=calculation.accrual_not_calculated_reason,
            alert_code=calculation.accrual_alert_code,
            direction=_accrual_direction(calculation.accrual_alert_code),
        ),
        _DetectionValues(
            monitor_type=MonitorType.TAX_BURDEN,
            calculation_status=burden_status,
            input_amount=_amount(calculation.cumulative_tax_payable),
            result_amount=None,
            difference_amount=None,
            tax_burden_rate=burden_rate,
            tax_burden_deviation=burden_deviation,
            reason=burden_reason,
            alert_code=burden_alert_code,
            direction=burden_direction,
        ),
        _DetectionValues(
            monitor_type=MonitorType.POTENTIAL_TAX_COST,
            calculation_status=calculation.potential_status,
            input_amount=_amount(calculation.potential_adjustment),
            result_amount=_amount(calculation.potential_tax_payable),
            difference_amount=_amount(calculation.potential_tax_cost),
            tax_burden_rate=None,
            tax_burden_deviation=None,
            reason=calculation.potential_not_calculated_reason,
            alert_code=calculation.potential_tax_cost_alert_code,
            direction=_potential_direction(calculation.potential_tax_cost),
        ),
    )


def _new_detection(
    *,
    context: _RunContext,
    values: _DetectionValues,
    calculation: QuarterlyResult,
    formula_substitution: dict[str, Any],
    lineage: dict[str, Any],
) -> DetectionRecord:
    return DetectionRecord(
        detection_key=(f"{context.run.id}:{context.snapshot.id}:{values.monitor_type.value}"),
        run_id=context.run.id,
        company_id=context.company.id,
        snapshot_id=context.snapshot.id,
        rule_version_id=context.rule.id,
        tax_master_version_id=context.master.id,
        monitor_type=values.monitor_type,
        calculation_status=CalculationStatus(values.calculation_status.value),
        input_amount=values.input_amount,
        result_amount=values.result_amount,
        difference_amount=values.difference_amount,
        rate_value=context.master.tax_rate,
        tax_burden_rate=values.tax_burden_rate,
        tax_burden_deviation=values.tax_burden_deviation,
        currency=calculation.currency,
        amount_scale=calculation.amount_scale,
        formula_substitution=deepcopy(formula_substitution),
        lineage=deepcopy(lineage),
        structured_output=_structured_output(values),
        not_calculated_reason=values.reason,
        alert_code=values.alert_code,
        direction=values.direction,
    )


def _new_case(
    *,
    context: _RunContext,
    detection: DetectionRecord,
    fingerprint: str,
) -> RiskCase:
    risk_amount, risk_rate = _case_values(detection)
    if detection.id is None or detection.direction is None:
        raise QuarterlyRunError(
            "ALERT_DETECTION_INVALID",
            "an alert detection must have a persisted id and direction",
        )
    if risk_amount is None and risk_rate is None:
        raise QuarterlyRunError(
            "ALERT_DETECTION_INVALID",
            "an alert detection must carry its monitor-specific risk value",
        )
    return RiskCase(
        fingerprint=fingerprint,
        company_id=context.company.id,
        latest_detection_id=detection.id,
        monitor_type=detection.monitor_type,
        status=RiskCaseStatus.NEW,
        risk_amount=risk_amount,
        risk_rate=risk_rate,
        currency=detection.currency,
        amount_scale=detection.amount_scale,
        risk_direction=detection.direction,
        priority=3,
        assignee=None,
        merged_into_case_id=None,
        lineage=deepcopy(detection.lineage),
        row_version=1,
    )


def _case_values(detection: DetectionRecord) -> tuple[Decimal | None, Decimal | None]:
    if detection.monitor_type == MonitorType.TAX_BURDEN:
        return (
            None,
            abs(detection.tax_burden_deviation)
            if detection.tax_burden_deviation is not None
            else None,
        )
    return (
        abs(detection.difference_amount) if detection.difference_amount is not None else None,
        None,
    )


def _is_newer_case_detection(
    uow: UnitOfWork,
    candidate_run: MonitoringRun,
    risk_case: RiskCase,
) -> bool:
    if risk_case.latest_detection_id is None:
        return True
    latest_run = uow.session.execute(
        select(MonitoringRun.created_at, MonitoringRun.id)
        .join(DetectionRecord, DetectionRecord.run_id == MonitoringRun.id)
        .where(DetectionRecord.id == risk_case.latest_detection_id)
    ).one_or_none()
    if latest_run is None:
        raise QuarterlyRunError(
            "CASE_LATEST_DETECTION_INVALID",
            "risk case latest detection does not resolve to a monitoring run",
        )
    return (candidate_run.created_at, candidate_run.id.int) > (
        latest_run.created_at,
        latest_run.id.int,
    )


def _refresh_case_summary(
    risk_case: RiskCase,
    detection: DetectionRecord,
) -> None:
    risk_amount, risk_rate = _case_values(detection)
    if detection.id is None or detection.direction is None:
        raise QuarterlyRunError(
            "ALERT_DETECTION_INVALID",
            "an alert detection must have a persisted id and direction",
        )
    if risk_amount is None and risk_rate is None:
        raise QuarterlyRunError(
            "ALERT_DETECTION_INVALID",
            "an alert detection must carry its monitor-specific risk value",
        )
    risk_case.latest_detection_id = detection.id
    risk_case.risk_amount = risk_amount
    risk_case.risk_rate = risk_rate
    risk_case.risk_direction = detection.direction
    risk_case.currency = detection.currency
    risk_case.amount_scale = detection.amount_scale
    risk_case.lineage = deepcopy(detection.lineage)
    risk_case.row_version += 1


def _existing_detections(
    uow: UnitOfWork,
    run_id: UUID,
    snapshot_id: UUID,
) -> tuple[DetectionRecord, ...]:
    return tuple(
        uow.session.scalars(
            select(DetectionRecord)
            .where(
                DetectionRecord.run_id == run_id,
                DetectionRecord.snapshot_id == snapshot_id,
            )
            .order_by(DetectionRecord.monitor_type, DetectionRecord.id)
        )
    )


def _assert_complete_retry(
    detections: tuple[DetectionRecord, ...],
    context: _ReplayContext,
) -> str:
    by_monitor = _by_monitor(detections)
    if len(detections) != len(MONITOR_ORDER) or set(by_monitor) != set(MONITOR_ORDER):
        raise QuarterlyRunError(
            "PARTIAL_DETECTION_SET",
            "an idempotent retry found an incomplete quarterly detection set",
        )
    if any(
        detection.run_id != context.run.id
        or detection.snapshot_id != context.snapshot.id
        or detection.company_id != context.snapshot.company_id
        or detection.rule_version_id != context.run.rule_version_id
        or detection.tax_master_version_id != context.snapshot.tax_master_version_id
        or detection.currency != context.snapshot.currency
        or detection.amount_scale != context.snapshot.amount_scale
        for detection in detections
    ):
        raise QuarterlyRunError(
            "DETECTION_IDENTITY_MISMATCH",
            "existing detections do not match the run's frozen evidence identity",
        )
    first_lineage = detections[0].lineage
    if any(detection.lineage != first_lineage for detection in detections[1:]):
        raise QuarterlyRunError(
            "DETECTION_IDENTITY_MISMATCH",
            "existing detections do not share one immutable frozen lineage",
        )
    company = first_lineage.get("company")
    snapshot = first_lineage.get("snapshot")
    rule = first_lineage.get("rule_version")
    master = first_lineage.get("tax_master_version")
    if not all(isinstance(value, dict) for value in (company, snapshot, rule, master)):
        raise QuarterlyRunError(
            "DETECTION_IDENTITY_MISMATCH",
            "existing detections lack their immutable identity lineage",
        )
    assert isinstance(company, dict)
    assert isinstance(snapshot, dict)
    assert isinstance(rule, dict)
    assert isinstance(master, dict)
    company_code = company.get("company_code")
    if (
        not isinstance(company_code, str)
        or not company_code.strip()
        or company.get("id") != str(context.snapshot.company_id)
        or snapshot.get("id") != str(context.snapshot.id)
        or snapshot.get("period") != context.snapshot.period.isoformat()
        or snapshot.get("checksum") != context.snapshot.checksum
        or snapshot.get("source_version_set_hash") != context.snapshot.source_version_set_hash
        or snapshot.get("snapshot_set_id") != str(context.snapshot_set.id)
        or rule.get("id") != str(context.run.rule_version_id)
        or master.get("id") != str(context.snapshot.tax_master_version_id)
        or master.get("currency") != context.snapshot.currency
        or master.get("amount_scale") != context.snapshot.amount_scale
    ):
        raise QuarterlyRunError(
            "DETECTION_IDENTITY_MISMATCH",
            "existing detection lineage does not match the frozen run identity",
        )
    return company_code


def _assert_replayable_run_status(run: MonitoringRun) -> None:
    if run.status not in {
        MonitoringRunStatus.RUNNING,
        MonitoringRunStatus.SUCCEEDED,
        MonitoringRunStatus.PARTIAL_SUCCESS,
        MonitoringRunStatus.FAILED,
    }:
        raise QuarterlyRunError(
            "MONITORING_RUN_NOT_RUNNING",
            "only running or terminal quarterly runs can replay complete detections",
        )


def _assert_first_execution_status(run: MonitoringRun) -> None:
    if run.status != MonitoringRunStatus.RUNNING:
        raise QuarterlyRunError(
            "MONITORING_RUN_NOT_RUNNING",
            "a quarterly company can execute initially only while its run is RUNNING",
        )


def _by_monitor(
    detections: tuple[DetectionRecord, ...],
) -> dict[MonitorType, DetectionRecord]:
    return {detection.monitor_type: detection for detection in detections}


def _case_ids_for_detections(
    uow: UnitOfWork,
    run: MonitoringRun,
    company_code: str,
    detections: tuple[DetectionRecord, ...],
) -> tuple[UUID, ...]:
    fingerprints = tuple(
        case_fingerprint(
            company_code,
            run.fiscal_year,
            run.quarter,
            detection.monitor_type.value,
        )
        for detection in (_by_monitor(detections)[monitor] for monitor in MONITOR_ORDER)
        if detection.alert_code is not None
    )
    if not fingerprints:
        return ()
    cases = {
        risk_case.fingerprint: risk_case.id
        for risk_case in uow.session.scalars(
            select(RiskCase).where(RiskCase.fingerprint.in_(fingerprints))
        )
    }
    if set(cases) != set(fingerprints):
        raise QuarterlyRunError(
            "DETECTION_CASE_INCONSISTENT",
            "alert detections exist without their risk cases",
        )
    return tuple(cases[fingerprint] for fingerprint in fingerprints)


def _detection_lineage(context: _RunContext) -> dict[str, Any]:
    snapshot_lineage = _canonical_json(context.snapshot.lineage)
    sources = snapshot_lineage.get("sources", [])
    metrics = snapshot_lineage.get("metrics", [])
    if not isinstance(sources, list) or not isinstance(metrics, list):
        raise QuarterlyRunError(
            "SNAPSHOT_LINEAGE_INVALID",
            "frozen snapshot source and metric lineage must be lists",
        )
    return {
        "company": {
            "id": str(context.company.id),
            "company_code": context.company.company_code,
        },
        "snapshot": {
            "id": str(context.snapshot.id),
            "period": context.snapshot.period.isoformat(),
            "checksum": context.snapshot.checksum,
            "source_version_set_hash": context.snapshot.source_version_set_hash,
            "snapshot_set_id": str(context.snapshot_set.id),
        },
        "rule_version": {
            "id": str(context.rule.id),
            "rule_code": context.rule.rule_code,
            "version": context.rule.version,
            "definition": _canonical_json(context.rule.definition),
        },
        "tax_master_version": {
            "id": str(context.master.id),
            "version": context.master.version,
            "source_batch_id": str(context.master.source_batch_id),
            "source_checksum": context.master.source_checksum,
            "source_row_number": context.master.source_row_number,
            "valid_from": context.master.valid_from.isoformat(),
            "valid_to": (
                context.master.valid_to.isoformat() if context.master.valid_to is not None else None
            ),
            "tax_rate": format(context.master.tax_rate, "f"),
            "loss_carryforward": format(context.master.loss_carryforward, "f"),
            "historical_average_tax_burden": format(
                context.master.average_tax_burden_rate_3y,
                "f",
            ),
            "currency": context.master.currency,
            "amount_scale": context.master.amount_scale,
        },
        "sources": deepcopy(sources),
        "metrics": deepcopy(metrics),
    }


def _structured_output(values: _DetectionValues) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _canonical_json(
            {
                "monitor_type": values.monitor_type,
                "calculation_status": values.calculation_status,
                "input_amount": values.input_amount,
                "result_amount": values.result_amount,
                "difference_amount": values.difference_amount,
                "tax_burden_rate": values.tax_burden_rate,
                "tax_burden_deviation": values.tax_burden_deviation,
                "not_calculated_reason": values.reason,
                "alert": values.alert_code is not None,
                "alert_code": values.alert_code,
                "direction": values.direction,
            }
        ),
    )


def _canonical_json(value: object) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise QuarterlyRunError(
            "NON_CANONICAL_LINEAGE",
            "quarterly evidence does not accept binary floating-point values",
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QuarterlyRunError(
                "NON_CANONICAL_LINEAGE",
                "quarterly evidence requires finite decimal values",
            )
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise QuarterlyRunError(
                "NON_CANONICAL_LINEAGE",
                "quarterly evidence requires timezone-aware timestamps",
            )
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _canonical_json(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise QuarterlyRunError(
                    "NON_CANONICAL_LINEAGE",
                    "quarterly evidence requires string object keys",
                )
            result[key] = _canonical_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    raise QuarterlyRunError(
        "NON_CANONICAL_LINEAGE",
        f"unsupported quarterly evidence value {type(value).__name__}",
    )


def _amount(value: Money | None) -> Decimal | None:
    return value.amount if value is not None else None


def _database_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite():
        raise QuarterlyRunError(
            "RATE_VALUE_OVERFLOW",
            "calculated tax burden does not fit NUMERIC(38,12)",
        )
    integral_digits = max(value.adjusted() + 1, 0) if value else 0
    if integral_digits > 26:
        raise QuarterlyRunError(
            "RATE_VALUE_OVERFLOW",
            "calculated tax burden does not fit NUMERIC(38,12)",
        )
    quantum = Decimal("0.000000000001")
    context = Context(
        prec=96,
        rounding=ROUND_HALF_UP,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        clamp=0,
    )
    try:
        stored = context.quantize(value, quantum)
    except InvalidOperation as error:
        raise QuarterlyRunError(
            "RATE_VALUE_OVERFLOW",
            "calculated tax burden does not fit NUMERIC(38,12)",
        ) from error
    stored_integral_digits = max(stored.adjusted() + 1, 0) if stored else 0
    if not stored.is_finite() or stored_integral_digits > 26:
        raise QuarterlyRunError(
            "RATE_VALUE_OVERFLOW",
            "calculated tax burden does not fit NUMERIC(38,12)",
        )
    return stored


def _accrual_direction(alert_code: str | None) -> str | None:
    if alert_code is None:
        return None
    return {
        "UNDER_ACCRUED": "UNDER",
        "OVER_ACCRUED": "OVER",
    }.get(alert_code)


def _burden_direction(alert_code: str | None) -> str | None:
    if alert_code is None:
        return None
    return {
        "TAX_BURDEN_HIGH": "HIGH",
        "TAX_BURDEN_LOW": "LOW",
    }.get(alert_code)


def _potential_direction(value: Money | None) -> str | None:
    if value is None or value.amount == 0:
        return None
    return "INCREASE" if value.amount > 0 else "DECREASE"


__all__ = [
    "QuarterlyRunError",
    "QuarterlyRunResult",
    "QuarterlyRunService",
    "assert_approved_quarterly_rule_manifest",
]
