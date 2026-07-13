SHELL := /bin/bash

.PHONY: test-backend test-web verify-governance verify-release verify-capacity verify-migrations security-check uat verify-rollback

test-backend:
	mkdir -p artifacts/acceptance
	cd backend && .venv/bin/pytest --junitxml=../artifacts/acceptance/backend.xml
	test -s artifacts/acceptance/backend.xml

test-web:
	mkdir -p artifacts/acceptance/web-test-results
	cd web && npm test -- --run && npm run build
	cd web && PLAYWRIGHT_JSON_OUTPUT_NAME=../artifacts/acceptance/web-test-results/results.json npx playwright test --reporter=json
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

