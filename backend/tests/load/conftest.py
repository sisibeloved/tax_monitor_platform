from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

import pytest


PROFILE_PATH = Path(__file__).parent / "profiles" / "126_companies.json"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--capacity-report",
        action="store",
        default=None,
        help="写入阶段4容量验收JSON制品",
    )


def pytest_configure(config: pytest.Config) -> None:
    config._capacity_evidence = {  # type: ignore[attr-defined]
        "schema_version": "phase-4-capacity-report-v1",
        "production_ready": False,
        "checks": {},
    }


@pytest.fixture(scope="session")
def capacity_profile() -> dict[str, Any]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def capacity_evidence(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.config._capacity_evidence  # type: ignore[attr-defined,no-any-return]


@pytest.fixture(scope="session", autouse=True)
def validate_capacity_report_parent(request: pytest.FixtureRequest) -> Iterator[None]:
    raw_path = request.config.getoption("--capacity-report")
    if raw_path:
        parent = Path(raw_path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise pytest.UsageError(f"容量报告父目录不可用：{parent}")
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    raw_path = session.config.getoption("--capacity-report")
    if not raw_path:
        return
    evidence = session.config._capacity_evidence  # type: ignore[attr-defined]
    checks = evidence.get("checks", {})
    evidence["pytest_exit_status"] = exitstatus
    evidence["production_ready"] = bool(checks) and all(
        bool(item.get("passed")) for item in checks.values()
    ) and exitstatus == 0
    destination = Path(raw_path).expanduser().resolve()
    destination.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
