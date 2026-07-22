from __future__ import annotations

from dataclasses import dataclass

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AdjustmentLabel,
    AdjustmentSubject,
    CheckStatus,
)


WELFARE_ACCOUNT_MIN = 6_600_080_000
WELFARE_ACCOUNT_MAX = 6_600_089_900
DONATION_ACCOUNT = "6711060000"


@dataclass(frozen=True, slots=True)
class RuleDecision:
    status: CheckStatus
    labels: tuple[AdjustmentLabel, ...]
    matched_keywords: tuple[str, ...]


def account_is_in_scope(subject: AdjustmentSubject, gl_account: str) -> bool:
    normalized = gl_account.strip()
    if subject is AdjustmentSubject.DONATION:
        return normalized == DONATION_ACCOUNT
    return normalized.isdigit() and WELFARE_ACCOUNT_MIN <= int(normalized) <= WELFARE_ACCOUNT_MAX


def classify_detail(subject: AdjustmentSubject, detail_text: str) -> RuleDecision:
    if subject is AdjustmentSubject.DONATION:
        return _classify_donation(detail_text)
    return _classify_welfare(detail_text)


def _classify_welfare(detail_text: str) -> RuleDecision:
    labels: list[AdjustmentLabel] = []
    keywords: list[str] = []

    if "客户礼品" in detail_text:
        labels.append(AdjustmentLabel.WELFARE_CUSTOMER_GIFT_REVIEW)
        keywords.append("客户礼品")

    entertainment_hits = [
        keyword for keyword in ("供应商", "政府接待", "商务宴请") if keyword in detail_text
    ]
    if "客户" in detail_text and "客户礼品" not in detail_text:
        entertainment_hits.append("客户")
    if entertainment_hits:
        labels.append(AdjustmentLabel.WELFARE_BUSINESS_ENTERTAINMENT)
        keywords.extend(entertainment_hits)

    education_hits = [
        keyword for keyword in ("培训费", "讲师费", "考试费") if keyword in detail_text
    ]
    if education_hits:
        labels.append(AdjustmentLabel.WELFARE_EMPLOYEE_EDUCATION)
        keywords.extend(education_hits)

    if "宣传赠品" in detail_text:
        labels.append(AdjustmentLabel.WELFARE_ADVERTISING_PROMOTION)
        keywords.append("宣传赠品")

    if not labels:
        return RuleDecision(
            CheckStatus.NORMAL,
            (AdjustmentLabel.WELFARE_REASONABLE,),
            (),
        )
    return RuleDecision(
        CheckStatus.ABNORMAL,
        tuple(labels),
        tuple(dict.fromkeys(keywords)),
    )


def _classify_donation(detail_text: str) -> RuleDecision:
    labels: list[AdjustmentLabel] = []
    keywords: list[str] = []
    if "赞助" in detail_text:
        labels.append(AdjustmentLabel.DONATION_SPONSORSHIP)
        keywords.append("赞助")

    advertising_hits = [
        keyword for keyword in ("冠名", "广告权益", "品牌露出") if keyword in detail_text
    ]
    if advertising_hits:
        labels.append(AdjustmentLabel.DONATION_ADVERTISING_PROMOTION)
        keywords.extend(advertising_hits)

    if not labels:
        return RuleDecision(
            CheckStatus.NORMAL,
            (AdjustmentLabel.DONATION_REASONABLE,),
            (),
        )
    return RuleDecision(
        CheckStatus.ABNORMAL,
        tuple(labels),
        tuple(dict.fromkeys(keywords)),
    )


__all__ = [
    "DONATION_ACCOUNT",
    "RuleDecision",
    "WELFARE_ACCOUNT_MAX",
    "WELFARE_ACCOUNT_MIN",
    "account_is_in_scope",
    "classify_detail",
]
