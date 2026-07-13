"""Vendor-neutral structured model boundary."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


class StructuredModelClient(Protocol):
    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[T],
    ) -> T: ...


__all__ = ["StructuredModelClient"]
