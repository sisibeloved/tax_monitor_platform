import pytest

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AdjustmentLabel,
    AdjustmentSubject,
    CheckStatus,
)
from tax_risk.application.tax_adjustment_accounts.rules import (
    account_is_in_scope,
    classify_detail,
)


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ("6600080000", True),
        ("6600089900", True),
        ("6600079999", False),
        ("6600089901", False),
        ("not-an-account", False),
    ],
)
def test_welfare_account_range_is_inclusive(account: str, expected: bool) -> None:
    assert account_is_in_scope(AdjustmentSubject.WELFARE, account) is expected


def test_donation_account_requires_exact_match() -> None:
    assert account_is_in_scope(AdjustmentSubject.DONATION, "6711060000") is True
    assert account_is_in_scope(AdjustmentSubject.DONATION, "6711060001") is False


@pytest.mark.parametrize(
    ("text", "label", "keyword"),
    [
        ("客户商务宴请", AdjustmentLabel.WELFARE_BUSINESS_ENTERTAINMENT, "客户"),
        ("供应商接待", AdjustmentLabel.WELFARE_BUSINESS_ENTERTAINMENT, "供应商"),
        ("员工培训费", AdjustmentLabel.WELFARE_EMPLOYEE_EDUCATION, "培训费"),
        ("市场宣传赠品", AdjustmentLabel.WELFARE_ADVERTISING_PROMOTION, "宣传赠品"),
        ("客户礼品", AdjustmentLabel.WELFARE_CUSTOMER_GIFT_REVIEW, "客户礼品"),
    ],
)
def test_welfare_special_text_is_labeled(
    text: str,
    label: AdjustmentLabel,
    keyword: str,
) -> None:
    result = classify_detail(AdjustmentSubject.WELFARE, text)

    assert result.status is CheckStatus.ABNORMAL
    assert label in result.labels
    assert keyword in result.matched_keywords


def test_welfare_can_retain_multiple_labels() -> None:
    result = classify_detail(AdjustmentSubject.WELFARE, "供应商培训费宣传赠品")

    assert result.labels == (
        AdjustmentLabel.WELFARE_BUSINESS_ENTERTAINMENT,
        AdjustmentLabel.WELFARE_EMPLOYEE_EDUCATION,
        AdjustmentLabel.WELFARE_ADVERTISING_PROMOTION,
    )


def test_employee_gift_remains_reasonable_welfare() -> None:
    result = classify_detail(AdjustmentSubject.WELFARE, "优秀教师年会礼品")

    assert result.status is CheckStatus.NORMAL
    assert result.labels == (AdjustmentLabel.WELFARE_REASONABLE,)


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ("公益项目赞助", (AdjustmentLabel.DONATION_SPONSORSHIP,)),
        ("活动冠名及品牌露出", (AdjustmentLabel.DONATION_ADVERTISING_PROMOTION,)),
        (
            "赞助项目并取得广告权益",
            (
                AdjustmentLabel.DONATION_SPONSORSHIP,
                AdjustmentLabel.DONATION_ADVERTISING_PROMOTION,
            ),
        ),
    ],
)
def test_donation_special_text_is_labeled(
    text: str,
    labels: tuple[AdjustmentLabel, ...],
) -> None:
    result = classify_detail(AdjustmentSubject.DONATION, text)

    assert result.status is CheckStatus.ABNORMAL
    assert result.labels == labels


def test_unmatched_donation_is_retained_as_reasonable() -> None:
    result = classify_detail(AdjustmentSubject.DONATION, "向公益基金会捐赠")

    assert result.status is CheckStatus.NORMAL
    assert result.labels == (AdjustmentLabel.DONATION_REASONABLE,)
