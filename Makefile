SHELL := /bin/bash

.PHONY: clean-test-artifacts test-backend test-backend-tiered test-backend-unit-coverage test-backend-refund-coverage test-infra test-web verify-governance verify-release verify-capacity verify-migrations security-check uat verify-rollback

clean-test-artifacts:
	rm -rf artifacts/acceptance
	mkdir -p artifacts/acceptance

test-backend:
	mkdir -p artifacts/acceptance
	cd backend && .venv/bin/pytest --junitxml=../artifacts/acceptance/backend.xml
	test -s artifacts/acceptance/backend.xml

test-backend-tiered:
	mkdir -p artifacts/acceptance
	cd backend && .venv/bin/pytest tests/e2e/test_dgc_tiered_sources.py \
		-m tiered_interface -s -o junit_logging=system-out \
		--junitxml=../artifacts/acceptance/backend-tiered.xml
	test -s artifacts/acceptance/backend-tiered.xml

test-backend-unit-coverage:
	mkdir -p artifacts/acceptance
	cd backend && .venv/bin/pytest tests/unit \
		--cov=tax_risk --cov-report=term-missing \
		--cov-report=xml:../artifacts/acceptance/backend-unit-coverage.xml \
		--cov-fail-under=60 \
		--junitxml=../artifacts/acceptance/backend-unit.xml
	test -s artifacts/acceptance/backend-unit.xml
	test -s artifacts/acceptance/backend-unit-coverage.xml

test-backend-refund-coverage:
	mkdir -p artifacts/acceptance
	cd backend && .venv/bin/pytest \
		tests/unit/adapters/test_lark_refund_base.py \
		tests/unit/application/test_refund_writebacks.py \
		tests/unit/workers/test_income_tax_refund_writeback_worker.py \
		tests/unit/test_lark_refund_runtime_wiring.py \
		--cov=tax_risk.adapters.lark.refund_base \
		--cov=tax_risk.application.refund_writebacks \
		--cov=tax_risk.workers.income_tax_refund_writebacks \
		--cov-report=term-missing \
		--cov-report=xml:../artifacts/acceptance/refund-writeback-coverage.xml \
		--cov-fail-under=95 \
		--junitxml=../artifacts/acceptance/refund-writeback.xml
	test -s artifacts/acceptance/refund-writeback.xml
	test -s artifacts/acceptance/refund-writeback-coverage.xml

test-infra:
	mkdir -p artifacts/acceptance
	backend/.venv/bin/pytest infra/tests \
		--junitxml=artifacts/acceptance/infra.xml
	test -s artifacts/acceptance/infra.xml

test-web:
	mkdir -p artifacts/acceptance/web-test-results
	cd web && npm run lint && npm test -- --run && npm run build
	cd web && PLAYWRIGHT_OUTPUT_DIR=../artifacts/acceptance/web-test-results/attachments \
		PLAYWRIGHT_JSON_OUTPUT_NAME=../artifacts/acceptance/web-test-results/results.json \
		npx playwright test --reporter=json --fail-on-flaky-tests
	cd web && node scripts/assert-playwright-results.mjs \
		../artifacts/acceptance/web-test-results/results.json
	test -s artifacts/acceptance/web-test-results/results.json

verify-governance:
	infra/scripts/verify_governance.sh

verify-release:
	infra/scripts/verify_release.sh

verify-capacity:
	COMPANY_FIXTURE="$${COMPANY_FIXTURE:-126}" infra/scripts/verify_capacity.sh

verify-migrations:
	infra/scripts/verify_migrations.sh

security-check:
	infra/scripts/security_check.sh

uat:
	SNAPSHOT_SET="$${SNAPSHOT_SET:-pilot-2026q2}" infra/scripts/run_uat.sh

verify-rollback:
	infra/scripts/rollback_drill.sh

