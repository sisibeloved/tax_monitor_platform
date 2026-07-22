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
from time import monotonic
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text

from tax_risk.domain.cases import case_fingerprint
from tax_risk.domain.money import Money, Rate
from tax_risk.domain.quarterly import (
    CalculationStatus as DomainCalculationStatus,
    DeferredTaxBaseFormula,
    DeferredTaxInputs,
    DeferredTaxResult,
    QuarterlyInputs,
    QuarterlyResult,
    calculate_deferred_tax,
    calculate_quarterly,
)
from tax_risk.observability.metrics import DEFAULT_METRICS
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
QUARTERLY_V1_MONITOR_ORDER: tuple[MonitorType, ...] = (
    MonitorType.ACCRUAL_ACCURACY,
    MonitorType.TAX_BURDEN,
    MonitorType.POTENTIAL_TAX_COST,
)
QUARTERLY_V2_MONITOR_ORDER: tuple[MonitorType, ...] = (
    MonitorType.ACCRUAL_ACCURACY,
    MonitorType.DEFERRED_TAX_ACCURACY,
    MonitorType.TAX_BURDEN,
    MonitorType.POTENTIAL_TAX_COST,
)
QUARTERLY_V3_MONITOR_ORDER = QUARTERLY_V2_MONITOR_ORDER
MONITOR_ORDER = QUARTERLY_V1_MONITOR_ORDER
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
        "current_tax_burden": (
            "0 if cumulative_revenue<=0 else cumulative_tax_payable/cumulative_revenue"
        ),
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
APPROVED_QUARTERLY_V2_MANIFEST: dict[str, object] = {
    "schema_version": "QUARTERLY_V2",
    "rounding_mode": "ROUND_HALF_UP",
    "formulas": {
        **cast(dict[str, object], APPROVED_QUARTERLY_MANIFEST["formulas"]),
        "deferred_tax_base": "loss_carryforward+cumulative_profit",
        "system_cumulative_deferred_tax": "deferred_tax_base*deferred_tax_rate",
        "current_year_deferred_tax_adjustment": (
            "system_cumulative_deferred_tax-sap_cumulative_deferred_tax_expense"
        ),
    },
    "alert_boundaries": {
        **cast(dict[str, object], APPROVED_QUARTERLY_MANIFEST["alert_boundaries"]),
        "deferred_tax_accuracy": "adjustment != 0",
    },
}
APPROVED_QUARTERLY_V2_MANIFEST_SHA256 = sha256(
    json.dumps(
        APPROVED_QUARTERLY_V2_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
APPROVED_QUARTERLY_V3_MANIFEST: dict[str, object] = {
    "schema_version": "QUARTERLY_V3",
    "rounding_mode": "ROUND_HALF_UP",
    "formulas": {
        **cast(dict[str, object], APPROVED_QUARTERLY_V2_MANIFEST["formulas"]),
        "deferred_tax_base": "loss_carryforward-cumulative_profit",
    },
    "alert_boundaries": {
        **cast(dict[str, object], APPROVED_QUARTERLY_V2_MANIFEST["alert_boundaries"]),
    },
}
APPROVED_QUARTERLY_V3_MANIFEST_SHA256 = sha256(
    json.dumps(
        APPROVED_QUARTERLY_V3_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
APPROVED_QUARTERLY_RULES: dict[
    str,
    tuple[dict[str, object], str, tuple[MonitorType, ...]],
] = {
    "QUARTERLY_V1": (
        APPROVED_QUARTERLY_MANIFEST,
        APPROVED_QUARTERLY_MANIFEST_SHA256,
        QUARTERLY_V1_MONITOR_ORDER,
    ),
    "QUARTERLY_V2": (
        APPROVED_QUARTERLY_V2_MANIFEST,
        APPROVED_QUARTERLY_V2_MANIFEST_SHA256,
        QUARTERLY_V2_MONITOR_ORDER,
    ),
    "QUARTERLY_V3": (
        APPROVED_QUARTERLY_V3_MANIFEST,
        APPROVED_QUARTERLY_V3_MANIFEST_SHA256,
        QUARTERLY_V3_MONITOR_ORDER,
    ),
}
APPROVED_QUARTERLY_RULE_VERSIONS = {
    "QUARTERLY_V1": "phase-1-reviewed",
    "QUARTERLY_V2": "deferred-tax-reviewed",
    "QUARTERLY_V3": "deferred-tax-loss-less-profit-reviewed",
}


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
    deferred_tax_rate: Decimal | None


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
                company_code, monitor_order = _assert_complete_retry(
                    existing,
                    replay_context,
                )
                _assert_replayable_run_status(replay_context.run)
                replay_case_ids = _case_ids_for_detections(
                    uow,
                    replay_context.run,
                    company_code,
                    existing,
                    monitor_order,
                )
                return QuarterlyRunResult(
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    detection_ids=tuple(
                        _by_monitor(existing)[monitor].id for monitor in monitor_order
                    ),
                    case_ids=replay_case_ids,
                    replayed=True,
                )
            context = _load_context(uow, run_id=run_id, snapshot_id=snapshot_id)
            _assert_first_execution_status(context.run)

            metrics = _snapshot_metrics(context.snapshot)
            inputs = _quarterly_inputs(
                context.snapshot,
                context.frozen_master,
                metrics,
            )
            formula_started = monotonic()
            calculation = calculate_quarterly(inputs)
            deferred_inputs: DeferredTaxInputs | None = None
            deferred_calculation: DeferredTaxResult | None = None
            if context.rule.rule_code in {"QUARTERLY_V2", "QUARTERLY_V3"}:
                deferred_inputs = _deferred_tax_inputs(
                    context.snapshot,
                    context.frozen_master,
                    metrics,
                )
                deferred_calculation = calculate_deferred_tax(
                    deferred_inputs,
                    base_formula=(
                        DeferredTaxBaseFormula.LOSS_PLUS_PROFIT
                        if context.rule.rule_code == "QUARTERLY_V2"
                        else DeferredTaxBaseFormula.LOSS_MINUS_PROFIT
                    ),
                )
            DEFAULT_METRICS.metric("tax_risk_formula_duration_seconds").observe(
                {"formula": "QUARTERLY_ALL"},
                monotonic() - formula_started,
            )
            substitutions = dict(calculation.formula_substitution)
            if deferred_calculation is not None:
                for key, value in deferred_calculation.formula_substitution.items():
                    if key in substitutions and substitutions[key] != value:
                        raise QuarterlyRunError(
                            "FORMULA_SUBSTITUTION_CONFLICT",
                            f"deferred-tax substitution {key!r} conflicts with quarterly inputs",
                        )
                    substitutions[key] = value
            formula_substitution = _canonical_json(substitutions)
            lineage = _detection_lineage(context)
            values = _detection_values(
                calculation,
                inputs,
                deferred_calculation=deferred_calculation,
                deferred_inputs=deferred_inputs,
            )

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
                    cast(int, context.run.quarter),
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
        or rule.rule_code not in APPROVED_QUARTERLY_RULES
        or rule.status != VersionStatus.PUBLISHED
        or rule.effective_from > snapshot.period
        or (rule.effective_to is not None and rule.effective_to < snapshot.period)
    ):
        raise QuarterlyRunError(
            "QUARTERLY_RULE_NOT_EFFECTIVE",
            "the run must pin an effective published reviewed quarterly rule version",
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

    approved = APPROVED_QUARTERLY_RULES.get(rule.rule_code)
    if approved is None:
        raise QuarterlyRunError(
            "QUARTERLY_RULE_MANIFEST_INVALID",
            "the pinned rule code is not an approved quarterly formula contract",
        )
    expected_manifest, expected_hash, _monitor_order = approved
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
        or stored_hash != expected_hash
        or manifest != expected_manifest
    ):
        raise QuarterlyRunError(
            "QUARTERLY_RULE_MANIFEST_INVALID",
            "the pinned rule does not match its fixed reviewed quarterly manifest",
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
        deferred_tax_rate = (
            _lineage_decimal(lineage, "deferred_tax_rate")
            if "deferred_tax_rate" in lineage
            else None
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
    optional_expected: dict[str, object] = {}
    if "source_file_name" in lineage:
        optional_expected["source_file_name"] = current.source_file_name
    if "imported_at" in lineage:
        try:
            imported_at = _canonical_utc_lineage_timestamp(lineage["imported_at"])
            current_imported_at = _canonical_json(current.created_at)
        except (TypeError, ValueError) as error:
            raise QuarterlyRunError(
                "FROZEN_MASTER_MISMATCH",
                "snapshot tax-master lineage contains an invalid import timestamp",
            ) from error
        optional_expected["imported_at"] = current_imported_at
        if imported_at != current_imported_at:
            raise QuarterlyRunError(
                "FROZEN_MASTER_MISMATCH",
                "current tax master no longer matches the snapshot's frozen lineage",
            )
    if (
        any(lineage.get(field) != value for field, value in expected.items())
        or any(lineage.get(field) != value for field, value in optional_expected.items())
        or tax_rate != current.tax_rate
        or loss_carryforward != current.loss_carryforward
        or average_tax_burden != current.average_tax_burden_rate_3y
        or deferred_tax_rate != current.deferred_tax_rate
    ):
        raise QuarterlyRunError(
            "FROZEN_MASTER_MISMATCH",
            "current tax master no longer matches the snapshot's frozen lineage",
        )
    return _FrozenMaster(
        tax_rate=tax_rate,
        loss_carryforward=loss_carryforward,
        average_tax_burden_rate_3y=average_tax_burden,
        deferred_tax_rate=deferred_tax_rate,
    )


def _lineage_decimal(lineage: dict[str, Any], field: str) -> Decimal:
    raw = lineage.get(field)
    if not isinstance(raw, str):
        raise TypeError(f"{field} must be an exact decimal string")
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _canonical_utc_lineage_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TypeError("lineage timestamp must be a canonical UTC string")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise ValueError("lineage timestamp is not ISO-8601") from error
    canonical = _canonical_json(parsed)
    if canonical != value:
        raise ValueError("lineage timestamp must use canonical UTC representation")
    return value


def _snapshot_metrics(snapshot: AccountingSnapshot) -> dict[str, Decimal]:
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
    return metrics


def _metric_money(
    snapshot: AccountingSnapshot,
    metrics: Mapping[str, Decimal],
    metric_code: str,
) -> Money:
    try:
        amount = metrics[metric_code]
    except KeyError as error:
        raise QuarterlyRunError(
            "SNAPSHOT_METRICS_MISSING",
            f"frozen snapshot is missing quarterly metric: {metric_code}",
        ) from error
    return Money.unrounded(
        amount,
        currency=snapshot.currency,
        scale=snapshot.amount_scale,
    )


def _quarterly_inputs(
    snapshot: AccountingSnapshot,
    master: _FrozenMaster,
    metrics: Mapping[str, Decimal],
) -> QuarterlyInputs:

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

    return QuarterlyInputs(
        cumulative_profit=_metric_money(snapshot, metrics, "cumulative_profit"),
        received_dividends=_metric_money(snapshot, metrics, "received_dividends"),
        fair_value_change=_metric_money(snapshot, metrics, "fair_value_change"),
        loss_carryforward=Money.unrounded(
            master.loss_carryforward,
            currency=snapshot.currency,
            scale=snapshot.amount_scale,
        ),
        tax_rate=Rate.from_fraction(master.tax_rate),
        prior_quarter_current_tax=_metric_money(
            snapshot, metrics, "prior_quarter_current_tax"
        ),
        current_quarter_current_tax=_metric_money(
            snapshot, metrics, "current_quarter_current_tax"
        ),
        cumulative_revenue=_metric_money(snapshot, metrics, "cumulative_revenue"),
        historical_average_tax_burden=Rate.from_fraction(master.average_tax_burden_rate_3y),
        other_payables_accrual=_metric_money(snapshot, metrics, "other_payables_accrual"),
        hesi_no_invoice=_metric_money(snapshot, metrics, "hesi_no_invoice"),
    )


def _deferred_tax_inputs(
    snapshot: AccountingSnapshot,
    master: _FrozenMaster,
    metrics: Mapping[str, Decimal],
) -> DeferredTaxInputs:
    if master.deferred_tax_rate is None:
        raise QuarterlyRunError(
            "DEFERRED_TAX_RATE_MISSING",
            "the selected quarterly rule requires a frozen company deferred-tax rate",
        )
    return DeferredTaxInputs(
        cumulative_profit=_metric_money(snapshot, metrics, "cumulative_profit"),
        loss_carryforward=Money.unrounded(
            master.loss_carryforward,
            currency=snapshot.currency,
            scale=snapshot.amount_scale,
        ),
        deferred_tax_rate=Rate.from_fraction(master.deferred_tax_rate),
        sap_cumulative_deferred_tax_expense=_metric_money(
            snapshot,
            metrics,
            "sap_cumulative_deferred_tax_expense",
        ),
    )


def _detection_values(
    calculation: QuarterlyResult,
    inputs: QuarterlyInputs,
    *,
    deferred_calculation: DeferredTaxResult | None = None,
    deferred_inputs: DeferredTaxInputs | None = None,
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

    values = (
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
    if deferred_calculation is None and deferred_inputs is None:
        return values
    if deferred_calculation is None or deferred_inputs is None:
        raise QuarterlyRunError(
            "DEFERRED_TAX_CALCULATION_INCOMPLETE",
            "deferred-tax inputs and result must be provided together",
        )
    deferred_values = _DetectionValues(
        monitor_type=MonitorType.DEFERRED_TAX_ACCURACY,
        calculation_status=deferred_calculation.status,
        input_amount=deferred_inputs.sap_cumulative_deferred_tax_expense.amount,
        result_amount=_amount(deferred_calculation.system_cumulative_deferred_tax),
        difference_amount=_amount(
            deferred_calculation.current_year_deferred_tax_adjustment
        ),
        tax_burden_rate=None,
        tax_burden_deviation=None,
        reason=deferred_calculation.not_calculated_reason,
        alert_code=deferred_calculation.alert_code,
        direction=_deferred_tax_direction(deferred_calculation.alert_code),
    )
    return (values[0], deferred_values, *values[1:])


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
        rate_value=(
            context.master.deferred_tax_rate
            if values.monitor_type == MonitorType.DEFERRED_TAX_ACCURACY
            else context.master.tax_rate
        ),
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
) -> tuple[str, tuple[MonitorType, ...]]:
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
    rule_code = rule.get("rule_code")
    approved = APPROVED_QUARTERLY_RULES.get(rule_code) if isinstance(rule_code, str) else None
    if approved is None:
        raise QuarterlyRunError(
            "DETECTION_IDENTITY_MISMATCH",
            "existing detections reference an unsupported quarterly rule code",
        )
    monitor_order = approved[2]
    by_monitor = _by_monitor(detections)
    if len(detections) != len(monitor_order) or set(by_monitor) != set(monitor_order):
        raise QuarterlyRunError(
            "PARTIAL_DETECTION_SET",
            "an idempotent retry found an incomplete quarterly detection set",
        )
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
    return company_code, monitor_order


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
    monitor_order: tuple[MonitorType, ...],
) -> tuple[UUID, ...]:
    fingerprints = tuple(
        case_fingerprint(
            company_code,
            run.fiscal_year,
            cast(int, run.quarter),
            detection.monitor_type.value,
        )
        for detection in (_by_monitor(detections)[monitor] for monitor in monitor_order)
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
    frozen_master = snapshot_lineage.get("tax_master")
    if not isinstance(frozen_master, dict):
        raise QuarterlyRunError(
            "SNAPSHOT_LINEAGE_INVALID",
            "frozen snapshot tax-master lineage must be an object",
        )
    for source in sources:
        if not isinstance(source, dict):
            continue
        batch = source.get("batch")
        if not isinstance(batch, dict):
            continue
        try:
            if "extraction_time" in batch:
                _canonical_utc_lineage_timestamp(batch["extraction_time"])
            if "payload_ref" in batch and not isinstance(
                batch["payload_ref"], (str, type(None))
            ):
                raise TypeError("payload_ref must be text or null")
        except (TypeError, ValueError) as error:
            raise QuarterlyRunError(
                "SNAPSHOT_LINEAGE_INVALID",
                "frozen snapshot source lineage contains invalid batch metadata",
            ) from error
    master_version_lineage = {
        "id": frozen_master["id"],
        "version": frozen_master["version"],
        "source_batch_id": frozen_master["source_batch_id"],
        "source_checksum": frozen_master["source_checksum"],
        "source_row_number": frozen_master["source_row_number"],
        "valid_from": frozen_master["valid_from"],
        "valid_to": frozen_master["valid_to"],
        "tax_rate": frozen_master["tax_rate"],
        "loss_carryforward": frozen_master["loss_carryforward"],
        "historical_average_tax_burden": frozen_master[
            "three_year_average_tax_burden"
        ],
        "currency": frozen_master["currency"],
        "amount_scale": frozen_master["amount_scale"],
    }
    if "deferred_tax_rate" in frozen_master:
        master_version_lineage["deferred_tax_rate"] = frozen_master[
            "deferred_tax_rate"
        ]
    for optional_field in ("source_file_name", "imported_at"):
        if optional_field in frozen_master:
            master_version_lineage[optional_field] = frozen_master[optional_field]
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
        "tax_master_version": master_version_lineage,
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


def _deferred_tax_direction(alert_code: str | None) -> str | None:
    if alert_code is None:
        return None
    return {
        "DEFERRED_TAX_TO_ACCRUE": "ACCRUE",
        "DEFERRED_TAX_TO_REVERSE": "REVERSE",
    }.get(alert_code)


def _potential_direction(value: Money | None) -> str | None:
    if value is None or value.amount == 0:
        return None
    return "INCREASE" if value.amount > 0 else "DECREASE"


__all__ = [
    "APPROVED_QUARTERLY_MANIFEST",
    "APPROVED_QUARTERLY_MANIFEST_SHA256",
    "APPROVED_QUARTERLY_V2_MANIFEST",
    "APPROVED_QUARTERLY_V2_MANIFEST_SHA256",
    "APPROVED_QUARTERLY_V3_MANIFEST",
    "APPROVED_QUARTERLY_V3_MANIFEST_SHA256",
    "APPROVED_QUARTERLY_RULE_VERSIONS",
    "QuarterlyRunError",
    "QuarterlyRunResult",
    "QuarterlyRunService",
    "assert_approved_quarterly_rule_manifest",
]
