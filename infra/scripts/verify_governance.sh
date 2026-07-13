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
  tests/integration/audit/test_append_only.py \
  tests/unit/model_gateway \
  tests/integration/model_gateway \
  tests/security/test_prompt_injection.py \
  --junitxml="${OUTPUT_DIR}/governance.xml" -q

test -s "${OUTPUT_DIR}/governance.xml"

