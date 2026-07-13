"""Vendor-neutral structured model boundary."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict


T = TypeVar("T", bound=BaseModel)


class ModelCallContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    candidate_key: str = "unknown"
    company_code: str = "unknown"
    model_version_id: str = "unknown"
    prompt_version_id: str = "unknown"
    case_library_version_id: str = "unknown"
    operator_id: str = "system"
    run_id: str = "unknown"


class StructuredModelClient(Protocol):
    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: dict[str, object],
        output_model: type[T],
    ) -> T: ...


@runtime_checkable
class ContextualStructuredModelClient(StructuredModelClient, Protocol):
    def with_context(self, context: ModelCallContext) -> StructuredModelClient: ...


__all__ = [
    "ContextualStructuredModelClient",
    "ModelCallContext",
    "StructuredModelClient",
]
