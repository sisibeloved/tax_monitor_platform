from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from tax_risk.application.business_entertainment.candidates import (
    CandidateInput,
    generate_candidates,
)
from tax_risk.domain.business_entertainment.lexicon import CandidateLexicon, load_lexicon


LEXICON_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "tax_risk"
    / "rules"
    / "business_entertainment_candidate_lexicon.v1.yaml"
)


def _raw_lexicon() -> dict[str, object]:
    return {
        "version": "v1",
        "monitor_type": "BUSINESS_ENTERTAINMENT",
        "effective_from": "2026-01-01",
        "status": "PUBLISHED",
        "signals": [
            {
                "signal_id": "INTERNAL_MEETING_MEAL",
                "canonical_phrase": "内部会议餐",
                "aliases": ["内部会议餐"],
                "allowed_fields": ["summary"],
                "priority": 100,
                "label_hints": ["MEETING_EXPENSE"],
            }
        ],
    }


def test_lexicon_rejects_unknown_keys_duplicate_ids_and_empty_aliases() -> None:
    unknown = _raw_lexicon()
    unknown["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        CandidateLexicon.model_validate(unknown)

    duplicate = _raw_lexicon()
    signals = duplicate["signals"]
    assert isinstance(signals, list)
    signals.append(deepcopy(signals[0]))
    with pytest.raises(ValidationError, match="duplicate signal_id"):
        CandidateLexicon.model_validate(duplicate)

    empty_alias = _raw_lexicon()
    empty_signals = empty_alias["signals"]
    assert isinstance(empty_signals, list)
    assert isinstance(empty_signals[0], dict)
    empty_signals[0]["aliases"] = [""]
    with pytest.raises(ValidationError):
        CandidateLexicon.model_validate(empty_alias)


def test_versioned_lexicon_contains_every_v08_high_recall_signal() -> None:
    lexicon = load_lexicon(LEXICON_PATH)

    phrases = {
        signal.canonical_phrase
        for signal in lexicon.signals
    }
    assert {
        "内部会议餐",
        "培训餐",
        "员工聚餐",
        "团建",
        "年会",
        "加班餐",
        "食堂",
        "员工福利",
        "会议通知",
        "签到",
        "议程",
        "培训班",
    } <= phrases
    assert lexicon.status == "PUBLISHED"


@pytest.mark.parametrize(
    ("phrase", "field"),
    [
        ("内部会议餐", "summary"),
        ("培训餐", "expense_reason"),
        ("员工聚餐", "reason"),
        ("团建", "reason"),
        ("年会", "summary"),
        ("加班餐", "purpose"),
        ("食堂", "item_description"),
        ("员工福利", "summary"),
        ("会议通知", "reason"),
        ("签到", "summary"),
        ("议程", "expense_reason"),
        ("培训班", "purpose"),
    ],
)
def test_every_known_positive_generates_candidate_and_preserves_quote(
    phrase: str,
    field: str,
) -> None:
    lexicon = load_lexicon(LEXICON_PATH)
    text = f"本次事项不是不相关，附件包含：{phrase}。"

    result = generate_candidates(
        CandidateInput(candidate_key=f"case-{phrase}", fields={field: text}),
        lexicon,
    )

    assert result.matches
    assert any(match.quoted_text == phrase for match in result.matches)
    assert all(match.field_name == field for match in result.matches)
    assert result.full_scan_included is True
    assert result.is_final_accounting_conclusion is False


def test_negation_never_suppresses_positive_hit_and_punctuation_is_normalized() -> None:
    lexicon = load_lexicon(LEXICON_PATH)
    text = "并非内部，会 议餐无需核查"

    result = generate_candidates(
        CandidateInput(candidate_key="negated", fields={"summary": text}),
        lexicon,
    )

    assert len(result.matches) >= 1
    match = next(item for item in result.matches if item.signal_id == "INTERNAL_MEETING_MEAL")
    assert match.start == text.index("内")
    assert match.end == text.index("餐") + 1
    assert match.quoted_text == "内部，会 议餐"


def test_disallowed_field_does_not_match_but_full_scan_channel_remains() -> None:
    lexicon = load_lexicon(LEXICON_PATH)

    result = generate_candidates(
        CandidateInput(
            candidate_key="field-boundary",
            fields={"unapproved_raw_field": "员工聚餐"},
        ),
        lexicon,
    )

    assert result.matches == ()
    assert result.full_scan_included is True
