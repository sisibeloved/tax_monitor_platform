"""Merge the latest tax-adjustment account check into the published web report."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from tax_risk.application.tax_adjustment_accounts.web_report import (  # noqa: E402
    merge_tax_adjustment_report,
)


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "web" / "public" / "real-validation-latest.json",
    )
    parser.add_argument(
        "--tax-adjustment-results",
        type=Path,
        default=(
            REPO_ROOT / "artifacts" / "acceptance" / "tax_adjustment_accounts_full_2026_06.json"
        ),
    )
    parser.add_argument(
        "--tax-adjustment-candidates",
        type=Path,
        default=(
            REPO_ROOT
            / "artifacts"
            / "acceptance"
            / "tax_adjustment_accounts_candidates_2026_06.json"
        ),
    )
    args = parser.parse_args()
    report_path = args.report.resolve()
    merged = merge_tax_adjustment_report(
        _load_json(report_path),
        result_path=args.tax_adjustment_results.resolve(),
        candidate_path=args.tax_adjustment_candidates.resolve(),
    )
    merged["generated_at"] = datetime.now(UTC).isoformat()
    _write_json(report_path, merged)
    print(f"result written: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
