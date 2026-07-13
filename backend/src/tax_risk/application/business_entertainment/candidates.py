"""High-recall screening candidates; matches are never accounting conclusions."""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from tax_risk.domain.business_entertainment.lexicon import CandidateLexicon


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    candidate_key: str = Field(min_length=1, max_length=512)
    fields: dict[str, str]


class CandidateMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str
    field_name: str
    start: int
    end: int
    quoted_text: str
    matched_phrase: str
    priority: int
    label_hints: tuple[str, ...]


class CandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str
    lexicon_version: str
    matches: tuple[CandidateMatch, ...]
    full_scan_included: bool = True
    is_final_accounting_conclusion: bool = False


def _normalized_with_offsets(value: str) -> tuple[str, tuple[int, ...]]:
    normalized: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(unicodedata.normalize("NFKC", value)):
        category = unicodedata.category(character)
        if character.isspace() or category.startswith("P"):
            continue
        normalized.append(character.casefold())
        offsets.append(index)
    return "".join(normalized), tuple(offsets)


def _normalized_phrase(value: str) -> str:
    return _normalized_with_offsets(value)[0]


def generate_candidates(
    item: CandidateInput,
    lexicon: CandidateLexicon,
) -> CandidateResult:
    matches: dict[tuple[str, str, int, int], CandidateMatch] = {}
    for field_name, text in sorted(item.fields.items()):
        normalized_text, offsets = _normalized_with_offsets(text)
        if not normalized_text:
            continue
        for signal in lexicon.signals:
            if field_name not in signal.allowed_fields:
                continue
            phrases = (signal.canonical_phrase, *signal.aliases)
            for phrase in phrases:
                normalized_phrase = _normalized_phrase(phrase)
                if not normalized_phrase:
                    continue
                search_from = 0
                while True:
                    normalized_start = normalized_text.find(normalized_phrase, search_from)
                    if normalized_start < 0:
                        break
                    normalized_end = normalized_start + len(normalized_phrase)
                    start = offsets[normalized_start]
                    end = offsets[normalized_end - 1] + 1
                    key = (signal.signal_id, field_name, start, end)
                    matches[key] = CandidateMatch(
                        signal_id=signal.signal_id,
                        field_name=field_name,
                        start=start,
                        end=end,
                        quoted_text=text[start:end],
                        matched_phrase=phrase,
                        priority=signal.priority,
                        label_hints=signal.label_hints,
                    )
                    search_from = normalized_start + 1
    ordered = tuple(
        sorted(
            matches.values(),
            key=lambda match: (
                -match.priority,
                match.field_name,
                match.start,
                match.end,
                match.signal_id,
            ),
        )
    )
    return CandidateResult(
        candidate_key=item.candidate_key,
        lexicon_version=lexicon.version,
        matches=ordered,
    )


__all__ = [
    "CandidateInput",
    "CandidateMatch",
    "CandidateResult",
    "generate_candidates",
]
