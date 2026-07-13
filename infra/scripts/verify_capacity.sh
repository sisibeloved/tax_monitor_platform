#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTEST="${PYTEST:-${ROOT_DIR}/backend/.venv/bin/pytest}"
COMPANY_FIXTURE="${COMPANY_FIXTURE:-126}"
mkdir -p "${OUTPUT_DIR}"

if [[ "${COMPANY_FIXTURE}" != "126" ]]; then
  echo "第四阶段固定容量验收仅允许 COMPANY_FIXTURE=126" >&2
  exit 2
fi

cd "${ROOT_DIR}/backend"
"${PYTEST}" tests/load -q \
  --capacity-report="${OUTPUT_DIR}/capacity-report.json" \
  --junitxml="${OUTPUT_DIR}/capacity.xml"

test -s "${OUTPUT_DIR}/capacity-report.json"
test -s "${OUTPUT_DIR}/capacity.xml"
"${ROOT_DIR}/backend/.venv/bin/python" -c 'import json, pathlib, sys; d=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert d["profile"]["company_count"] == 126; assert d["checks"]["failure_isolation"]["success_rate"] >= 0.98; assert d["checks"]["t_plus_2"]["passed"] is True; assert d["production_ready"] is True' "${OUTPUT_DIR}/capacity-report.json"
