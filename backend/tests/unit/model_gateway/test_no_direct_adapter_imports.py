from __future__ import annotations

import ast
from pathlib import Path


def test_only_model_gateway_constructs_enterprise_adapter() -> None:
    source_root = Path(__file__).parents[3] / "src" / "tax_risk"
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root).as_posix()
        if relative in {
            "adapters/model/enterprise_structured_client.py",
            "model_gateway/service.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "tax_risk.adapters.model.enterprise_structured_client"
            ):
                violations.append(relative)
    assert violations == []

