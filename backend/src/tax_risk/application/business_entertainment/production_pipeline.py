"""Database-backed composition of the governed business-entertainment pipeline."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from tax_risk.adapters.model.enterprise_structured_client import (
    EnterpriseStructuredModelClient,
    ModelCallContext,
)
from tax_risk.application.business_entertainment.agent import (
    BusinessEntertainmentProfessionalAgent,
)
from tax_risk.application.business_entertainment.candidates import (
    CandidateInput,
    CandidateResult,
    generate_candidates,
)
from tax_risk.application.business_entertainment.company_scope import (
    BusinessEntertainmentScopeService,
)
from tax_risk.application.business_entertainment.evaluation_items import (
    BusinessEvaluationSource,
    ExactEvidenceRelation,
    SapEvaluationSource,
    build_evaluation_items,
    build_sap_coverage_items,
)
from tax_risk.application.business_entertainment.evidence_review import (
    BusinessEntertainmentEvidencePack,
    build_business_evidence_pack,
    review_and_assemble_detection,
)
from tax_risk.application.business_entertainment.linker import (
    BusinessEvidence,
    ExactEvidenceLinker,
    SapEvidence,
)
from tax_risk.application.business_entertainment.service import (
    BusinessEntertainmentRunError,
    BusinessEntertainmentRunRequest,
    PublishedCompanyInput,
)
from tax_risk.application.business_entertainment.source_loader import (
    EntertainmentSnapshotSourceLoader,
)
from tax_risk.application.semantic.account_dictionary import (
    SuggestedAccountDictionaryService,
)
from tax_risk.application.semantic.detection_router import (
    RoutingOutcome,
    SemanticCaseRouter,
)
from tax_risk.application.semantic.model_client import StructuredModelClient
from tax_risk.domain.business_entertainment.evaluation import (
    BusinessEntertainmentEvaluationItem,
    SapLinkCoverageItem,
    SapLinkStatus,
)
from tax_risk.domain.business_entertainment.lexicon import (
    CandidateLexicon,
    LexiconStatus,
    load_lexicon,
)
from tax_risk.domain.semantic.contracts import SemanticDetection, SemanticVersionSet
from tax_risk.persistence.business_entertainment_models import (
    BusinessEntertainmentEvaluation,
    BusinessEntertainmentSourceObservation,
)
from tax_risk.persistence.ingest_models import Company, SourceRecord
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_models import SemanticArtifactVersion
from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSet,
    SnapshotSetMember,
    SnapshotSetStatus,
    SnapshotSource,
)


UowFactory = Callable[[], UnitOfWork]
_BUSINESS_DATASETS = frozenset(
    {
        "hesi_business_entertainment",
        "oa_business_entertainment",
        "oa_self_procurement",
        "oa_material_requisition",
    }
)
_TEXT_FIELDS = frozenset(
    {
        "summary",
        "expense_reason",
        "reason",
        "purpose",
        "item_description",
        "material_description",
        "recipient_category",
    }
)
_SUSPICIOUS_LABELS = frozenset(
    {"MEETING_EXPENSE", "EMPLOYEE_EDUCATION", "EMPLOYEE_WELFARE"}
)


@dataclass(slots=True)
class _PipelineState:
    snapshot_id: UUID
    sap_link_sources: tuple[SapEvidence, ...]
    sap_evaluation_sources: tuple[SapEvaluationSource, ...]
    business_link_sources: tuple[BusinessEvidence, ...]
    business_evaluation_sources: tuple[BusinessEvaluationSource, ...]
    source_payloads: dict[UUID, tuple[str, dict[str, object]]]
    document_dates: dict[UUID, date]
    exact_relations: tuple[ExactEvidenceRelation, ...] = ()
    evaluation_items: tuple[BusinessEntertainmentEvaluationItem, ...] = ()
    coverage_items: tuple[SapLinkCoverageItem, ...] = ()
    candidate_results: dict[str, CandidateResult] = field(default_factory=dict)


class DatabaseBusinessEntertainmentPipeline:
    """Resolve every stage from immutable database records and governed versions."""

    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        model_client: StructuredModelClient | None,
        lexicon: CandidateLexicon | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_client = model_client
        self._lexicon = lexicon or load_lexicon(
            Path(__file__).parents[2]
            / "rules"
            / "business_entertainment_candidate_lexicon.v1.yaml"
        )
        self._states: dict[str, _PipelineState] = {}

    def resolve_scope(self, period_end: date) -> tuple[str, ...]:
        try:
            return BusinessEntertainmentScopeService(self._uow_factory).resolve(
                effective_on=period_end
            ).company_codes
        except Exception as error:
            error_code = getattr(error, "error_code", "BUSINESS_ENTERTAINMENT_SCOPE_NOT_READY")
            raise BusinessEntertainmentRunError(error_code, str(error)) from error

    def load_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
    ) -> PublishedCompanyInput:
        self._validate_governed_versions(request)
        with self._uow_factory() as uow:
            row = uow.session.execute(
                select(SnapshotSet, AccountingSnapshot)
                .join(
                    SnapshotSetMember,
                    SnapshotSetMember.snapshot_set_id == SnapshotSet.id,
                )
                .join(Company, Company.id == SnapshotSetMember.company_id)
                .join(
                    AccountingSnapshot,
                    AccountingSnapshot.id == SnapshotSetMember.snapshot_id,
                )
                .where(
                    SnapshotSet.id == request.snapshot_set_id,
                    Company.company_code == request.company_code,
                )
            ).one_or_none()
            if row is None:
                raise BusinessEntertainmentRunError(
                    "SNAPSHOT_MEMBER_MISSING",
                    "snapshot set does not contain the requested company",
                )
            snapshot_set, snapshot = row
            status = (
                snapshot_set.status.value
                if isinstance(snapshot_set.status, SnapshotSetStatus)
                else str(snapshot_set.status)
            )
            published_at = snapshot_set.published_at
            snapshot_id = snapshot.id
            if status != SnapshotSetStatus.PUBLISHED.value or published_at is None:
                return PublishedCompanyInput(
                    snapshot_status=status,
                    published_at=published_at,
                    source_record_count=0,
                    exact_link_count=0,
                    fuzzy_hint_count=0,
                    conflict_count=0,
                    sap_linked_candidate_keys=(),
                    business_unlinked_candidate_keys=(),
                    standalone_sap_keys=(),
                )
            business_rows = uow.session.execute(
                select(BusinessEntertainmentSourceObservation, SourceRecord)
                .join(
                    SourceRecord,
                    SourceRecord.id
                    == BusinessEntertainmentSourceObservation.source_record_id,
                )
                .join(
                    SnapshotSource,
                    SnapshotSource.ingest_batch_id
                    == BusinessEntertainmentSourceObservation.ingest_batch_id,
                )
                .where(
                    SnapshotSource.snapshot_id == snapshot_id,
                    BusinessEntertainmentSourceObservation.company_code
                    == request.company_code,
                    BusinessEntertainmentSourceObservation.dataset_code.in_(
                        _BUSINESS_DATASETS
                    ),
                    BusinessEntertainmentSourceObservation.fiscal_year
                    == request.period_end.year,
                    BusinessEntertainmentSourceObservation.period
                    <= request.period_end.month,
                )
                .order_by(
                    BusinessEntertainmentSourceObservation.dataset_code,
                    BusinessEntertainmentSourceObservation.source_record_key,
                )
            ).all()
            business_link_sources = tuple(
                _business_link_source(observation, source.payload)
                for observation, source in business_rows
            )
            business_evaluation_sources = tuple(
                BusinessEvaluationSource(
                    source_record_id=observation.source_record_id,
                    dataset_code=observation.dataset_code,
                    company_code=observation.company_code,
                    document_id=observation.document_id,
                    line_id=observation.line_id,
                    document_date=observation.document_date,
                    amount=observation.amount,
                    currency=observation.currency,
                )
                for observation, _ in business_rows
            )
            business_source_payloads = {
                observation.source_record_id: (
                    observation.dataset_code,
                    dict(source.payload),
                )
                for observation, source in business_rows
            }
            document_dates = {
                observation.source_record_id: observation.document_date
                for observation, _ in business_rows
            }
            business_row_count = len(business_rows)

        sap_result = EntertainmentSnapshotSourceLoader(
            self._uow_factory
        ).load_sap_vouchers(
            snapshot_set_id=request.snapshot_set_id,
            company_code=request.company_code,
            period_end=request.period_end,
        )
        if sap_result.issues:
            issue = sap_result.issues[0]
            raise BusinessEntertainmentRunError(issue.error_code, issue.remediation)

        sap_link_sources = tuple(
            SapEvidence(
                observation_id=item.observation_id,
                source_record_id=item.source_record_id,
                snapshot_id=item.snapshot_id,
                company_code=item.company_code,
                document_number=item.document_number,
                line_item=item.line_item,
                posting_date=item.posting_date,
                amount=item.amount,
                assignment=item.assignment,
                reference=item.reference,
            )
            for item in sap_result.records
        )
        sap_evaluation_sources = tuple(
            SapEvaluationSource(
                observation_id=item.observation_id,
                source_record_id=item.source_record_id,
                snapshot_id=item.snapshot_id,
                snapshot_period_end=request.period_end,
                company_code=item.company_code,
                fiscal_year=item.fiscal_year,
                period=item.period,
                posting_date=item.posting_date,
                document_number=item.document_number,
                line_item=item.line_item,
                current_account_code=item.current_account_code,
                current_account_name=item.current_account_name,
                amount=item.amount,
                currency=item.currency,
            )
            for item in sap_result.records
        )
        source_payloads = business_source_payloads
        source_payloads.update(
            {
                item.source_record_id: (
                    "sap_business_entertainment",
                    {"summary": item.summary},
                )
                for item in sap_result.records
            }
        )
        self._states[request.idempotency_key] = _PipelineState(
            snapshot_id=snapshot_id,
            sap_link_sources=sap_link_sources,
            sap_evaluation_sources=sap_evaluation_sources,
            business_link_sources=business_link_sources,
            business_evaluation_sources=business_evaluation_sources,
            source_payloads=source_payloads,
            document_dates=document_dates,
        )
        return PublishedCompanyInput(
            snapshot_status=status,
            published_at=published_at,
            source_record_count=len(sap_result.records) + business_row_count,
            exact_link_count=0,
            fuzzy_hint_count=0,
            conflict_count=0,
            sap_linked_candidate_keys=(),
            business_unlinked_candidate_keys=(),
            standalone_sap_keys=(),
        )

    def link_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
        inputs: PublishedCompanyInput,
        *,
        idempotency_key: str,
    ) -> PublishedCompanyInput:
        state = self._state(idempotency_key)
        link_result = ExactEvidenceLinker(self._uow_factory).link_and_persist(
            state.sap_link_sources,
            state.business_link_sources,
            snapshot_id=state.snapshot_id,
        )
        with self._uow_factory() as uow:
            persisted_links = tuple(
                link
                for link in uow.business_entertainment_scope.evidence_links_for_snapshot(
                    state.snapshot_id
                )
                if link.company_code == request.company_code
                and link.relation_quality == "EXACT"
            )
            relations = tuple(
                ExactEvidenceRelation(
                    evidence_link_id=link.id,
                    company_code=link.company_code,
                    source_record_id=link.source_record_id,
                    target_record_id=link.target_record_id,
                    relation_kind=link.relation_kind,
                    snapshot_id=link.snapshot_id,
                )
                for link in persisted_links
            )
            persisted_link_count = len(persisted_links)
        built_evaluations = build_evaluation_items(
            state.snapshot_id,
            state.sap_evaluation_sources,
            state.business_evaluation_sources,
            relations,
        )
        evaluations = tuple(
            item for item in built_evaluations if item.source_mode.value == "SAP_LINKED"
        ) + tuple(
            item
            for item in built_evaluations
            if item.source_mode.value == "BUSINESS_DOCUMENT_UNLINKED"
        )
        coverages = build_sap_coverage_items(
            state.snapshot_id,
            state.sap_evaluation_sources,
            relations,
        )
        state.exact_relations = relations
        state.evaluation_items = evaluations
        state.coverage_items = coverages
        return inputs.model_copy(
            update={
                "exact_link_count": persisted_link_count,
                "fuzzy_hint_count": len(link_result.fuzzy_hints),
                "conflict_count": len(link_result.conflicts),
                "sap_linked_candidate_keys": tuple(
                    item.candidate_key
                    for item in evaluations
                    if item.source_mode.value == "SAP_LINKED"
                ),
                "business_unlinked_candidate_keys": tuple(
                    item.candidate_key
                    for item in evaluations
                    if item.source_mode.value == "BUSINESS_DOCUMENT_UNLINKED"
                ),
                "standalone_sap_keys": _standalone_sap_keys(state, coverages),
            }
        )

    def persist_coverages(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        linked_candidate_keys: tuple[str, ...],
        standalone_sap_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> int:
        state = self._state(idempotency_key)
        expected_linked = tuple(
            item.candidate_key
            for item in state.evaluation_items
            if item.source_mode.value == "SAP_LINKED"
        )
        if linked_candidate_keys != expected_linked:
            raise BusinessEntertainmentRunError(
                "PIPELINE_STAGE_IDENTITY_MISMATCH",
                "linked evaluation identities changed between stages",
            )
        expected_standalone_count = sum(
            item.link_status is SapLinkStatus.UNLINKED for item in state.coverage_items
        )
        if len(standalone_sap_keys) != expected_standalone_count:
            raise BusinessEntertainmentRunError(
                "PIPELINE_STAGE_IDENTITY_MISMATCH",
                "standalone SAP identities changed between stages",
            )
        with self._uow_factory() as uow:
            uow.business_entertainment_scope.persist_sap_link_coverages(
                state.coverage_items
            )
            uow.business_entertainment_scope.persist_evaluations(
                [_evaluation_model(item) for item in state.evaluation_items]
            )
            uow.commit()
        return len(state.coverage_items)

    def generate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        evaluation_candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[str, ...]:
        state = self._state(idempotency_key)
        expected = tuple(item.candidate_key for item in state.evaluation_items)
        if evaluation_candidate_keys != expected:
            raise BusinessEntertainmentRunError(
                "PIPELINE_STAGE_IDENTITY_MISMATCH",
                "evaluation identities changed before candidate generation",
            )
        for item in state.evaluation_items:
            fields = _combined_candidate_fields(state, item.canonical_source_record_id)
            state.candidate_results[item.candidate_key] = generate_candidates(
                CandidateInput(candidate_key=item.candidate_key, fields=fields),
                self._lexicon,
            )
        return expected

    def evaluate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[SemanticDetection, ...]:
        state = self._state(idempotency_key)
        if self._model_client is None:
            raise BusinessEntertainmentRunError(
                "SEMANTIC_MODEL_NOT_CONFIGURED",
                "a governed structured model client is required",
            )
        if candidate_keys != tuple(item.candidate_key for item in state.evaluation_items):
            raise BusinessEntertainmentRunError(
                "PIPELINE_STAGE_IDENTITY_MISMATCH",
                "candidate identities changed before Agent evaluation",
            )
        versions = SemanticVersionSet(
            rule_version_id=request.rule_version_id,
            model_version_id=request.model_version_id,
            prompt_version_id=request.prompt_version_id,
            case_library_version_id=request.case_library_version_id,
            account_dictionary_version=request.account_dictionary_version_id,
        )
        dictionary = SuggestedAccountDictionaryService(self._uow_factory)
        detections: list[SemanticDetection] = []
        for item in state.evaluation_items:
            evidence_pack = _evidence_pack(state, item)
            client = _contextual_client(
                self._model_client,
                request=request,
                candidate_key=item.candidate_key,
            )
            judgment = asyncio.run(
                BusinessEntertainmentProfessionalAgent(client).evaluate(
                    evidence_pack=evidence_pack,
                    current_account_name=item.current_account_name,
                    document_date=state.document_dates[
                        item.canonical_source_record_id
                    ],
                    versions=versions,
                )
            )
            detections.append(
                review_and_assemble_detection(
                    evaluation_item=item,
                    judgment=judgment,
                    evidence_pack=evidence_pack,
                    versions=versions,
                    account_validator=lambda account_id, label: _account_is_valid(
                        dictionary,
                        request=request,
                        account_id=account_id,
                        semantic_label=label,
                    ),
                )
            )
        return tuple(detections)

    def route_detections(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        detections: tuple[SemanticDetection, ...],
        idempotency_key: str,
    ) -> tuple[int, int, int]:
        self._state(idempotency_key)
        router = SemanticCaseRouter(self._uow_factory)
        results = tuple(
            router.route(detection, suspicious_labels=_SUSPICIOUS_LABELS)
            for detection in detections
        )
        return (
            len(results),
            sum(result.outcome is RoutingOutcome.EVIDENCE_TASK for result in results),
            sum(result.outcome is RoutingOutcome.RISK_CASE for result in results),
        )

    def _validate_governed_versions(
        self,
        request: BusinessEntertainmentRunRequest,
    ) -> None:
        if (
            self._lexicon.version != request.lexicon_version
            or self._lexicon.status is not LexiconStatus.PUBLISHED
            or self._lexicon.effective_from > request.period_end
        ):
            raise BusinessEntertainmentRunError(
                "CANDIDATE_LEXICON_NOT_PUBLISHED",
                "requested candidate lexicon is not the effective published version",
            )
        with self._uow_factory() as uow:
            actual_artifacts = {
                artifact_type: version
                for artifact_type, version in uow.session.execute(
                    select(
                        SemanticArtifactVersion.artifact_type,
                        SemanticArtifactVersion.version,
                    ).where(
                        SemanticArtifactVersion.status == "PUBLISHED",
                        SemanticArtifactVersion.effective_from <= request.period_end,
                        SemanticArtifactVersion.effective_to >= request.period_end,
                    )
                ).all()
            }
            dictionary = uow.semantic.get_account_dictionary_by_name(
                request.account_dictionary_version_id
            )
            dictionary_state = (
                None
                if dictionary is None
                else (
                    dictionary.status,
                    dictionary.effective_from,
                    dictionary.effective_to,
                )
            )
        expected_artifacts = {
            "MODEL": request.model_version_id,
            "PROMPT": request.prompt_version_id,
            "CASE_LIBRARY": request.case_library_version_id,
        }
        if actual_artifacts != expected_artifacts:
            raise BusinessEntertainmentRunError(
                "SEMANTIC_ARTIFACTS_NOT_PUBLISHED",
                "requested model, prompt, and case-library versions are not effective",
            )
        if (
            dictionary_state is None
            or dictionary_state[0] != "PUBLISHED"
            or not dictionary_state[1]
            <= request.period_end
            <= dictionary_state[2]
        ):
            raise BusinessEntertainmentRunError(
                "ACCOUNT_DICTIONARY_NOT_PUBLISHED",
                "requested account dictionary is not effective and published",
            )

    def _state(self, idempotency_key: str) -> _PipelineState:
        state = self._states.get(idempotency_key)
        if state is None:
            raise BusinessEntertainmentRunError(
                "PIPELINE_STATE_MISSING",
                "pipeline stages must execute in the governed order",
            )
        return state


def _business_link_source(
    observation: BusinessEntertainmentSourceObservation,
    payload: dict[str, object],
) -> BusinessEvidence:
    return BusinessEvidence(
        source_record_id=observation.source_record_id,
        dataset_code=observation.dataset_code,
        company_code=observation.company_code,
        document_id=observation.document_id,
        line_id=observation.line_id,
        document_date=observation.document_date,
        amount=observation.amount,
        related_oa_id=_optional_text(payload.get("related_oa_id")),
        sap_document_number=_optional_text(payload.get("sap_document_number")),
        sap_line_item=_optional_text(payload.get("sap_line_item")),
        parent_oa_id=observation.parent_oa_id,
        parent_hesi_id=observation.parent_hesi_id,
    )


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _evaluation_model(
    item: BusinessEntertainmentEvaluationItem,
) -> BusinessEntertainmentEvaluation:
    return BusinessEntertainmentEvaluation(
        candidate_key=item.candidate_key,
        company_code=item.company_code,
        fiscal_year=item.fiscal_year,
        period=item.period,
        source_mode=item.source_mode.value,
        canonical_record_type=item.canonical_record_type.value,
        canonical_source_record_id=item.canonical_source_record_id,
        sap_observation_id=item.sap_observation_id,
        amount=item.amount,
        amount_source=item.amount_source.value,
        snapshot_id=item.snapshot_id,
    )


def _standalone_sap_keys(
    state: _PipelineState,
    coverages: tuple[SapLinkCoverageItem, ...],
) -> tuple[str, ...]:
    source_by_observation = {
        source.observation_id: source for source in state.sap_evaluation_sources
    }
    return tuple(
        source_by_observation[coverage.sap_observation_id].business_key
        for coverage in coverages
        if coverage.link_status is SapLinkStatus.UNLINKED
    )


def _related_source_ids(state: _PipelineState, canonical_source_id: UUID) -> tuple[UUID, ...]:
    adjacency: dict[UUID, set[UUID]] = defaultdict(set)
    for relation in state.exact_relations:
        adjacency[relation.source_record_id].add(relation.target_record_id)
        adjacency[relation.target_record_id].add(relation.source_record_id)
    visited = {canonical_source_id}
    queue: deque[UUID] = deque((canonical_source_id,))
    while queue:
        current = queue.popleft()
        for related in adjacency[current]:
            if related not in visited:
                visited.add(related)
                queue.append(related)
    return tuple(sorted(visited, key=str))


def _evidence_fields(
    state: _PipelineState,
    canonical_source_id: UUID,
) -> tuple[tuple[str, str, UUID], ...]:
    fields: list[tuple[str, str, UUID]] = []
    for source_id in _related_source_ids(state, canonical_source_id):
        source = state.source_payloads.get(source_id)
        if source is None:
            continue
        _, payload = source
        for field_name in sorted(_TEXT_FIELDS):
            value = payload.get(field_name)
            if isinstance(value, str) and value.strip():
                fields.append((field_name, value.strip(), source_id))
    if not fields:
        raise BusinessEntertainmentRunError(
            "AUTHORIZED_EVIDENCE_MISSING",
            "candidate has no authorized text evidence",
        )
    return tuple(fields)


def _combined_candidate_fields(
    state: _PipelineState,
    canonical_source_id: UUID,
) -> dict[str, str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for field_name, value, _ in _evidence_fields(state, canonical_source_id):
        grouped[field_name].append(value)
    return {field_name: "\n".join(values) for field_name, values in grouped.items()}


def _evidence_pack(
    state: _PipelineState,
    item: BusinessEntertainmentEvaluationItem,
) -> BusinessEntertainmentEvidencePack:
    return build_business_evidence_pack(
        candidate_key=item.candidate_key,
        snapshot_id=item.snapshot_id,
        canonical_source_record_id=item.canonical_source_record_id,
        fields=_evidence_fields(state, item.canonical_source_record_id),
    )


def _contextual_client(
    client: StructuredModelClient,
    *,
    request: BusinessEntertainmentRunRequest,
    candidate_key: str,
) -> StructuredModelClient:
    if isinstance(client, EnterpriseStructuredModelClient):
        return client.with_context(
            ModelCallContext(
                candidate_key=candidate_key,
                company_code=request.company_code,
                model_version_id=request.model_version_id,
                prompt_version_id=request.prompt_version_id,
                case_library_version_id=request.case_library_version_id,
                operator_id="business-entertainment-worker",
                run_id=str(request.run_id),
            )
        )
    return client


def _account_is_valid(
    service: SuggestedAccountDictionaryService,
    *,
    request: BusinessEntertainmentRunRequest,
    account_id: str,
    semantic_label: str,
) -> bool:
    try:
        service.resolve_account(
            dictionary_version=request.account_dictionary_version_id,
            account_id=account_id,
            monitor_type="BUSINESS_ENTERTAINMENT",
            semantic_label=semantic_label,
            effective_on=request.period_end,
        )
    except Exception:
        return False
    return True


__all__ = ["DatabaseBusinessEntertainmentPipeline"]
