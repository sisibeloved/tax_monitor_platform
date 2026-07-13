"""Deterministic structured client available only in explicit test environments."""

from __future__ import annotations

from collections import deque
from typing import TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


class FakeStructuredModelClient:
    def __init__(self, responses: list[dict[str, object]], *, environment: str) -> None:
        if environment != "test":
            raise RuntimeError("fake structured model client is restricted to test")
        self._responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[T],
    ) -> T:
        if not self._responses:
            raise RuntimeError("fake structured model client has no response")
        self.calls.append(
            {
                "system_prompt_checksum_only": len(system_prompt),
                "input_fields": tuple(sorted(input_json)),
                "output_model": output_model.__name__,
            }
        )
        return output_model.model_validate(self._responses.popleft())


__all__ = ["FakeStructuredModelClient"]
