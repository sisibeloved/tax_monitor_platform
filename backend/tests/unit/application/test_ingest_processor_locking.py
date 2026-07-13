from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    CompanyMasterRow,
)
from tax_risk.application.ingest_processors import CompanyMasterProcessor, FinancialProcessor
from tax_risk.persistence.ingest_models import Company, CompanyLifecycle


EVENT_TIME = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)


def _company(code: str) -> Company:
    return Company(
        id=uuid4(),
        company_code=code,
        company_name=f"Company {code}",
        lifecycle=CompanyLifecycle.ACTIVE,
        master_data_updated_at=EVENT_TIME,
        lifecycle_changed_at=EVENT_TIME,
    )


class _LockOrderRepository:
    def __init__(self) -> None:
        self.companies = {code: _company(code) for code in ("A", "B")}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def lock_companies_exclusive(self, company_codes: set[str]) -> dict[str, Company | None]:
        ordered = tuple(sorted(company_codes))
        self.calls.append(("exclusive", ordered))
        return {code: self.companies.get(code) for code in ordered}

    def lock_companies_shared(self, company_codes: set[str]) -> dict[str, Company | None]:
        ordered = tuple(sorted(company_codes))
        self.calls.append(("shared", ordered))
        return {code: self.companies.get(code) for code in ordered}

    # Legacy single-code methods make the pre-fix implementation observable.
    def lock_company_code(self, company_code: str) -> None:
        self.calls.append(("legacy-exclusive", (company_code,)))

    def get_company_by_code(self, company_code: str, **_: object) -> Company | None:
        self.calls.append(("legacy-select", (company_code,)))
        return self.companies.get(company_code)

    def add_company(self, company: Company) -> None:
        self.companies[company.company_code] = company


def _uow(repository: _LockOrderRepository) -> SimpleNamespace:
    return SimpleNamespace(ingest=repository)


def _batch(dataset_code: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        source="TEST",
        source_batch_key=uuid4().hex,
        dataset_code=dataset_code,
        period=date(2026, 6, 30),
        currency="CNY",
        amount_scale=2,
    )


def test_company_master_materializes_rows_then_takes_one_sorted_exclusive_lock_set() -> None:
    repository = _LockOrderRepository()
    rows = [
        AdapterRow(
            row_number=index,
            value=CompanyMasterRow(
                source_record_key=f"master-{code}",
                company_code=code,
                company_name=f"Company {code}",
                lifecycle="ACTIVE",
                extracted_at=EVENT_TIME,
            ),
            error=None,
        )
        for index, code in enumerate(("B", "A"), start=2)
    ]

    CompanyMasterProcessor().process(
        rows,
        uow=_uow(repository),  # type: ignore[arg-type]
        batch=_batch("company_master"),  # type: ignore[arg-type]
        checksum="a" * 64,
    )

    assert repository.calls == [("exclusive", ("A", "B"))]


def test_financial_materializes_rows_then_takes_one_sorted_shared_lock_set() -> None:
    repository = _LockOrderRepository()
    rows = [
        AdapterRow(
            row_number=index,
            value=CanonicalFinancialRow(
                source_record_key=f"financial-{code}",
                company_code=code,
                fiscal_year=2026,
                period=date(2026, 6, 30),
                currency="CNY",
                amount_scale=2,
                metric_code="cumulative_profit",
                amount=Decimal("1.00"),
                extracted_at=EVENT_TIME,
            ),
            error=None,
        )
        for index, code in enumerate(("B", "A"), start=2)
    ]

    result = FinancialProcessor().process(
        rows,
        uow=_uow(repository),  # type: ignore[arg-type]
        batch=_batch("quarterly_metric"),  # type: ignore[arg-type]
        checksum="b" * 64,
    )

    assert result.accepted_count == 2
    assert repository.calls == [("shared", ("A", "B"))]


def test_financial_processor_errors_preserve_safe_company_and_metric_context() -> None:
    repository = _LockOrderRepository()
    rows = [
        AdapterRow(
            row_number=2,
            value=CanonicalFinancialRow(
                source_record_key="duplicate",
                company_code="A",
                fiscal_year=2026,
                period=date(2026, 6, 30),
                currency="CNY",
                amount_scale=2,
                metric_code="cumulative_profit",
                amount=Decimal("1.00"),
                extracted_at=EVENT_TIME,
            ),
            error=None,
        ),
        AdapterRow(
            row_number=3,
            value=CanonicalFinancialRow(
                source_record_key="duplicate",
                company_code="A",
                fiscal_year=2026,
                period=date(2026, 6, 30),
                currency="CNY",
                amount_scale=2,
                metric_code="received_dividends",
                amount=Decimal("2.00"),
                extracted_at=EVENT_TIME,
            ),
            error=None,
        ),
        AdapterRow(
            row_number=4,
            value=CanonicalFinancialRow(
                source_record_key="metadata",
                company_code="B",
                fiscal_year=2026,
                period=date(2026, 6, 30),
                currency="USD",
                amount_scale=2,
                metric_code="fair_value_change",
                amount=Decimal("3.00"),
                extracted_at=EVENT_TIME,
            ),
            error=None,
        ),
        AdapterRow(
            row_number=5,
            value=CanonicalFinancialRow(
                source_record_key="unknown",
                company_code="C",
                fiscal_year=2026,
                period=date(2026, 6, 30),
                currency="CNY",
                amount_scale=2,
                metric_code="cumulative_revenue",
                amount=Decimal("4.00"),
                extracted_at=EVENT_TIME,
            ),
            error=None,
        ),
    ]

    result = FinancialProcessor().process(
        rows,
        uow=_uow(repository),  # type: ignore[arg-type]
        batch=_batch("quarterly_metric"),  # type: ignore[arg-type]
        checksum="c" * 64,
    )

    assert [error.error_code for error in result.errors] == [
        "DUPLICATE_SOURCE_RECORD_KEY",
        "BATCH_METADATA_MISMATCH",
        "UNKNOWN_COMPANY",
    ]
    assert [error.context for error in result.errors] == [
        (("company_code", "A"), ("metric_code", "received_dividends")),
        (("company_code", "B"), ("metric_code", "fair_value_change")),
        (("company_code", "C"), ("metric_code", "cumulative_revenue")),
    ]
