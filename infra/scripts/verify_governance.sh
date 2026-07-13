#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTEST="${PYTEST:-${ROOT_DIR}/backend/.venv/bin/pytest}"
mkdir -p "${OUTPUT_DIR}"

cd "${ROOT_DIR}/backend"
"${PYTEST}" \
  tests/unit/security \
  tests/integration/security \
  tests/unit/audit \
  tests/integration/audit \
  tests/unit/model_gateway \
  tests/integration/model_gateway \
  tests/security/test_prompt_injection.py \
  tests/unit/test_config_security.py \
  tests/unit/workers/test_export_worker_scope.py \
  tests/unit/workers/test_monthly_worker_scope.py \
  tests/integration/api/test_cases_scope.py \
  tests/integration/exports/test_download_reauthorization.py \
  tests/integration/exports/test_export_scope.py \
  tests/integration/workers/test_business_entertainment_worker.py \
  tests/integration/workers/test_monthly_semantic_batch_eager.py \
  tests/integration/workers/test_quarterly_batch_eager.py \
  --junitxml="${OUTPUT_DIR}/governance.xml" -q

test -s "${OUTPUT_DIR}/governance.xml"
