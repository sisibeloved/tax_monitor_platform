"""Compatibility imports for the canonical persistence boundaries."""

from tax_risk.persistence.models import Base
from tax_risk.persistence.repositories import engine, session_factory

__all__ = ["Base", "engine", "session_factory"]
