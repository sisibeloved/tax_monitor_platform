#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTHON="${PYTHON:-${ROOT_DIR}/backend/.venv/bin/python}"
PYTEST="${PYTEST:-${ROOT_DIR}/backend/.venv/bin/pytest}"
SNAPSHOT_SET="${SNAPSHOT_SET:-pilot-2026q2}"
UAT_APPROVALS_JSON="${UAT_APPROVALS_JSON:-{}}"
UAT_EVIDENCE_SCOPE="${UAT_EVIDENCE_SCOPE:-LOCAL_SYNTHETIC}"
mkdir -p "${OUTPUT_DIR}"

cd "${ROOT_DIR}/backend"
"${PYTEST}" tests/evaluation \
  --junitxml="${OUTPUT_DIR}/uat-evaluation.xml" -q

if [[ "${UAT_REQUIRE_PRODUCTION_READY:-false}" == "true" ]]; then
  REQUIRE_PRODUCTION_READY="--require-production-ready"
else
  REQUIRE_PRODUCTION_READY=""
fi
"${PYTHON}" -m tax_risk.release.scorecard \
  --artifact-dir "${OUTPUT_DIR}" \
  --snapshot-set "${SNAPSHOT_SET}" \
  --approvals-json "${UAT_APPROVALS_JSON}" \
  --evidence-scope "${UAT_EVIDENCE_SCOPE}" \
  --output "${OUTPUT_DIR}/uat-scorecard.json" \
  ${REQUIRE_PRODUCTION_READY}

test -s "${OUTPUT_DIR}/uat-evaluation.xml"
test -s "${OUTPUT_DIR}/uat-scorecard.json"
"${PYTHON}" -c 'import json, pathlib, sys; d=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert d["technical_ready"] is True; assert d["evidence_scope"] in {"LOCAL_SYNTHETIC", "PILOT_PRODUCTION"}' "${OUTPUT_DIR}/uat-scorecard.json"
