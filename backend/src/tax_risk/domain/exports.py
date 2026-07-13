"""Asynchronous export job lifecycle contracts."""

from __future__ import annotations

from enum import StrEnum


class ExportJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class ExportType(StrEnum):
    BUSINESS_ENTERTAINMENT = "BUSINESS_ENTERTAINMENT"


__all__ = ["ExportJobStatus", "ExportType"]
