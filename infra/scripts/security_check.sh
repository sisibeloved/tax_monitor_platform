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
"${PYTEST}" tests/security tests/unit/model_gateway tests/unit/security \
  --junitxml="${OUTPUT_DIR}/security.xml" -q
"${PYTHON}" -c 'import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({"static_analysis": True, "type_check": True, "dependency_consistency": True, "adversarial_tests": True, "authorization_rls_isolation": True, "audit_immutability": True, "external_semantic_index_configured": False, "unresolved_high_severity_findings": 0}, ensure_ascii=False, indent=2) + "\n")' "${OUTPUT_DIR}/security.json"

test -s "${OUTPUT_DIR}/security.xml"
test -s "${OUTPUT_DIR}/security.json"
