#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTHON="${PYTHON:-${ROOT_DIR}/backend/.venv/bin/python}"
PYTEST="${PYTEST:-${ROOT_DIR}/backend/.venv/bin/pytest}"
mkdir -p "${OUTPUT_DIR}"

cd "${ROOT_DIR}/backend"
"${PYTEST}" tests/unit/release tests/integration/release tests/evaluation \
  --junitxml="${OUTPUT_DIR}/release.xml" -q
"${PYTHON}" -m tax_risk.release.reporting \
  --ci-evidence \
  --output-dir "${OUTPUT_DIR}" \
  --repository-root "${ROOT_DIR}"

test -s "${OUTPUT_DIR}/release.xml"
test -s "${OUTPUT_DIR}/replay-report.json"
test -s "${OUTPUT_DIR}/release-manifest.json"
test -s "${OUTPUT_DIR}/release-signature.json"
test -s "${OUTPUT_DIR}/signed-manifest.json"
"${PYTHON}" -c 'import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text()); assert d["verification_passed"] is True; assert d["signing_mode"] == "CI_EPHEMERAL_NOT_FOR_PRODUCTION"' "${OUTPUT_DIR}/signed-manifest.json"

