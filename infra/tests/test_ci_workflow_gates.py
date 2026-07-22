"""Regression contracts for CI evidence and release provenance gates."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
MAKEFILE = ROOT / "Makefile"
GITIGNORE = ROOT / ".gitignore"
STALE_WEB_RESULT = ROOT / "artifacts" / "acceptance" / "web-test-results" / "results.json"


def test_ci_runs_every_new_quality_gate_from_a_clean_evidence_directory() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    for command in (
        "make clean-test-artifacts",
        "make test-infra",
        "make test-backend-unit-coverage",
        "make test-backend-refund-coverage",
        "make test-backend",
        "make test-web",
    ):
        assert command in workflow
    assert "--cov-fail-under=60" in makefile
    assert "--cov-fail-under=95" in makefile
    assert "npm run lint" in makefile
    assert "--fail-on-flaky-tests" in makefile
    assert "scripts/assert-playwright-results.mjs" in makefile
    assert "if-no-files-found: error" in workflow


def test_generated_playwright_evidence_is_not_stored_as_current_source_evidence() -> None:
    ignored = GITIGNORE.read_text(encoding="utf-8").splitlines()

    assert "artifacts/acceptance/web-test-results/" in ignored
    assert not STALE_WEB_RESULT.exists()


def test_package_publish_permissions_are_isolated_to_verified_main_pushes() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    global_permissions, jobs = workflow.split("jobs:", maxsplit=1)
    verify_job, publish_job = jobs.split("  publish-image:", maxsplit=1)

    assert "contents: read" in global_permissions
    assert "packages: write" not in global_permissions
    assert "id-token: write" not in global_permissions
    assert "packages: write" not in verify_job
    assert "needs: verify" in publish_job
    assert "github.event_name == 'push'" in publish_job
    assert "github.ref == 'refs/heads/main'" in publish_job
    assert "packages: write" in publish_job
    assert "actions/attest-build-provenance@v2" in publish_job
    assert "org.opencontainers.image.revision=${{ github.sha }}" in publish_job
    assert "name: ci-image-provenance" in publish_job


def test_release_fails_closed_on_ci_commit_image_or_attestation_mismatch() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert 'workflow_id: "ci.yml"' in workflow
    assert 'run.event === "push"' in workflow
    assert "name: ci-image-provenance" in workflow
    assert 'ci_provenance["source_commit"] != expected_commit' in workflow
    assert 'ci_provenance["application_image_digest"] != expected_digest' in workflow
    assert 'manifest["application_image_digest"] != expected_digest' in workflow
    assert 'gh attestation verify "oci://${IMAGE_REFERENCE}"' in workflow
