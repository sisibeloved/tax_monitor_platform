#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTHON="${PYTHON:-${ROOT_DIR}/backend/.venv/bin/python}"

CANDIDATE_MANIFEST="${CANDIDATE_MANIFEST:-${OUTPUT_DIR}/signed-manifest.json}"
PREVIOUS_MANIFEST="${PREVIOUS_MANIFEST:-${OUTPUT_DIR}/previous-signed-manifest.json}"
BACKUP_ID="${BACKUP_ID:-acceptance-backup-2026q2-001}"
ENVIRONMENT="${ROLLBACK_ENVIRONMENT:-acceptance}"
APPROVED_CHANGE_ID="${APPROVED_CHANGE_ID:-CHG-LOCAL-PHASE4-ROLLBACK}"
REQUESTED_BY="${ROLLBACK_REQUESTED_BY:-local-release-operator}"
APPROVED_BY="${ROLLBACK_APPROVED_BY:-local-operations-owner}"
RESTORE_TARGET="${ISOLATED_RESTORE_TARGET:-isolated-phase4-restore}"
REPRESENTATIVE_COMPANY="${REPRESENTATIVE_COMPANY:-C001}"
REPORT_PATH="${ROLLBACK_REPORT_PATH:-${OUTPUT_DIR}/rollback-report.json}"

mkdir -p "${OUTPUT_DIR}"
test -s "${CANDIDATE_MANIFEST}"
test -s "${PREVIOUS_MANIFEST}"

CANDIDATE_HASH="$(${PYTHON} -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()[:16])' "${CANDIDATE_MANIFEST}")"
CHECKPOINT_PATH="${ROLLBACK_CHECKPOINT_PATH:-${OUTPUT_DIR}/rollback-checkpoint-${CANDIDATE_HASH}.json}"

"${PYTHON}" -m tax_risk.release.rollback \
  --candidate-manifest "${CANDIDATE_MANIFEST}" \
  --previous-manifest "${PREVIOUS_MANIFEST}" \
  --backup-id "${BACKUP_ID}" \
  --affected-batch "acceptance-quarterly-2026q2" \
  --affected-batch "acceptance-monthly-2026-06" \
  --environment "${ENVIRONMENT}" \
  --approved-change-id "${APPROVED_CHANGE_ID}" \
  --requested-by "${REQUESTED_BY}" \
  --approved-by "${APPROVED_BY}" \
  --checkpoint "${CHECKPOINT_PATH}" \
  --report "${REPORT_PATH}" \
  --isolated-restore-target "${RESTORE_TARGET}" \
  --representative-company "${REPRESENTATIVE_COMPANY}"

test -s "${CHECKPOINT_PATH}"
test -s "${REPORT_PATH}"
"${PYTHON}" -c 'import json, pathlib, sys; d=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert d["recovery_verified"] is True; assert d["duplicate_risk_exposures"] == 0; assert d["candidate_manifest_sha256"] != d["selected_manifest_sha256"]' "${REPORT_PATH}"

