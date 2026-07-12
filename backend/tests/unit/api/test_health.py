from fastapi.testclient import TestClient

from tax_risk.main import create_app
from tax_risk.config import Settings


def test_health_reports_service_ready() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "tax-risk"}


def test_tax_master_xlsx_resource_limits_are_wired_from_settings() -> None:
    app = create_app(
        settings=Settings(
            tax_master_xlsx_max_zip_members=17,
            tax_master_xlsx_max_total_uncompressed_bytes=18_000,
            tax_master_xlsx_max_member_uncompressed_bytes=9_000,
            tax_master_xlsx_max_compression_ratio=19,
            tax_master_xlsx_max_worksheet_rows=120,
            tax_master_xlsx_max_worksheet_cells=840,
        )
    )

    limits = app.state.tax_master_xlsx_limits
    assert limits.max_zip_members == 17
    assert limits.max_total_uncompressed_bytes == 18_000
    assert limits.max_member_uncompressed_bytes == 9_000
    assert limits.max_compression_ratio == 19
    assert limits.max_worksheet_rows == 120
    assert limits.max_worksheet_cells == 840
