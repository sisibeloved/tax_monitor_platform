"""PostgreSQL persistence package.

Use ``persistence.models.Base`` and ``persistence.repositories.UnitOfWork`` as
the canonical registry and transaction boundary.
"""

from __future__ import annotations

from tax_risk.persistence.models import Base


def register_models() -> None:
    """Load every focused model module into the canonical Base registry."""

    from tax_risk.persistence import (
        business_entertainment_models,
        ingest_models,
        master_models,
        risk_models,
        semantic_models,
        snapshot_models,
    )

    _ = (
        business_entertainment_models,
        ingest_models,
        master_models,
        risk_models,
        semantic_models,
        snapshot_models,
    )


register_models()


__all__ = ["Base", "register_models"]
