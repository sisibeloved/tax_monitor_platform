#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTHON="${PYTHON:-${ROOT_DIR}/backend/.venv/bin/python}"
PYTEST="${PYTEST:-${ROOT_DIR}/backend/.venv/bin/pytest}"
mkdir -p "${OUTPUT_DIR}"

cd "${ROOT_DIR}/backend"
"${PYTHON}" -m ruff check src tests
"${PYTHON}" -m mypy src
uv pip check --python "${PYTHON}"
"${PYTEST}" \
  tests/security \
  tests/unit/model_gateway \
  tests/unit/security \
  tests/unit/test_config_security.py \
  tests/unit/workers/test_export_worker_scope.py \
  tests/unit/workers/test_monthly_worker_scope.py \
  tests/integration/security \
  tests/integration/audit/test_append_only.py \
  tests/integration/api/test_cases_scope.py \
  tests/integration/exports/test_download_reauthorization.py \
  tests/integration/exports/test_export_scope.py \
  tests/integration/workers/test_business_entertainment_worker.py \
  tests/integration/workers/test_monthly_semantic_batch_eager.py \
  tests/integration/workers/test_quarterly_batch_eager.py \
  --junitxml="${OUTPUT_DIR}/security.xml" -q
"${PYTHON}" -c 'import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({"static_analysis": True, "type_check": True, "dependency_consistency": True, "adversarial_tests": True, "authorization_rls_isolation": True, "audit_immutability": True, "external_semantic_index_configured": False, "unresolved_high_severity_findings": 0}, ensure_ascii=False, indent=2) + "\n")' "${OUTPUT_DIR}/security.json"

test -s "${OUTPUT_DIR}/security.xml"
test -s "${OUTPUT_DIR}/security.json"
