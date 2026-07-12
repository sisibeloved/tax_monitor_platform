"""Operational cardinality limits shared by snapshot API and application layers."""

MAX_SNAPSHOT_SOURCE_BATCHES = 1_000
MAX_SNAPSHOT_SET_MEMBERS = 1_000


__all__ = ["MAX_SNAPSHOT_SET_MEMBERS", "MAX_SNAPSHOT_SOURCE_BATCHES"]
