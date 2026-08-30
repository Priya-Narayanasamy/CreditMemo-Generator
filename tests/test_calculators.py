"""Phase 2 tests: the calculators.

No LLM is involved in any calculation, so every one of these is exact. Money is
integer cents and ratios are Decimal - the type assertions here are load-bearing,
not decoration.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.tools.calculators import (
    IncomeResult,
    LoanSplitTerms,
    PayslipFigures,
    annualise_payg_income,
    calculate_lvr,
    calculate_serviceability,
    format_cents,
    format_ratio_as_percent,
    monthly_repayment,
    proposed_annual_repayments,
)


# --- LVR --------------------------------------------------------------------


@pytest.mark.parametrize(
    "loan, valuation, expected",
    [
        (585_000_00, 780_000_00, "0.7500"),
        (930_000_00, 1_240_000_00, "0.7500"),
        (697_000_00, 820_000_00, "0.8500"),
        (405_000_00, 540_000_00, "0.7500"),
        (1_170_000_00, 1_800_000_00, "0.6500"),
        (1, 3, "0.3333"),
        (2, 3, "0.6667"),
    ],
)
def test_calculate_lvr(loan, valuation, expected):
    result = calculate_lvr(loan, valuation)

    assert isinstance(result, Decimal)
    assert result == Decimal(expected)


def test_lvr_returns_decimal_never_float():
    assert not isinstance(calculate_lvr(1_00, 2_00), float)


def test_lvr_rejects_non_integer_money():
    with pytest.raises(TypeError):
        calculate_lvr(585_000.00, 780_000_00)
    with pytest.raises(TypeError):
        calculate_lvr(585_000_00, Decimal("780000"))


def test_lvr_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        calculate_lvr(100_00, 0)
    with pytest.raises(ValueError):
        calculate_lvr(-1, 100_00)


# --- PAYG income ------------------------------------------------------------


def slip(index: int, gross: int, ytd: int, name: str | None = None) -> PayslipFigures:
    """A fortnightly payslip, the periods running consecutively from index 1."""
    return PayslipFigures(
        source_filename=name or f"scan_000{index}.pdf",
        period_start=date.fromordinal(date(2026, 1, 9).toordinal() + (index - 1) * 14 - 13),
        period_end=date.fromordinal(date(2026, 1, 9).toordinal() + (index - 1) * 14),
        gross_for_period=gross,
        ytd_gross=ytd,
    )


def test_consistent_payslips_annualise():
    gross = 4_150_00
    result = annualise_payg_income([
        slip(1, gross, gross * 13),
        slip(2, gross, gross * 14),
        slip(3, gross, gross * 15),
    ])

    assert result.status == "resolved"
    assert result.annual_gross == gross * 26
    assert result.periods_per_year == 26
    assert result.conflicts == []
    assert isinstance(result.annual_gross, int)


def test_inconsistent_ytd_returns_a_conflict_and_never_averages():
    """The seeded defect 5 case. Two sources disagreeing is not an arithmetic
    problem to smooth over."""
    gross = 6_200_00
    result = annualise_payg_income([
        slip(1, gross, gross * 13),
        slip(2, gross, gross * 14 + 1_840_00),
        slip(3, gross, gross * 15),
    ])

    assert result.status == "conflict"
    assert result.annual_gross is None
    assert [c.kind for c in result.conflicts] == ["ytd_not_sequential", "ytd_not_sequential"]
    assert len(result.source_filenames) == 3

    for conflict in result.conflicts:
        assert len(conflict.sources) == 2
        assert conflict.description


def test_varying_period_gross_is_a_conflict():
    result = annualise_payg_income([
        slip(1, 4_000_00, 4_000_00 * 13),
        slip(2, 4_500_00, 4_000_00 * 13 + 4_500_00),
    ])

    assert result.status == "conflict"
    assert "gross_varies" in {c.kind for c in result.conflicts}


def test_ytd_absent_does_not_manufacture_a_conflict():
    gross = 3_300_00
    slips = [slip(1, gross, gross * 13), slip(2, gross, gross * 14)]
    for s in slips:
        s.ytd_gross = None

    result = annualise_payg_income(slips)

    assert result.status == "resolved"
    assert result.annual_gross == gross * 26


def test_a_single_payslip_is_insufficient_not_a_conflict():
    result = annualise_payg_income([slip(1, 4_150_00, 4_150_00 * 13)])

    assert result.status == "insufficient_data"
    assert result.annual_gross is None
    assert result.conflicts == []


def test_no_payslips_is_insufficient_data():
    result = annualise_payg_income([])

    assert result.status == "insufficient_data"
    assert result.annual_gross is None


def test_payslip_order_does_not_matter():
    gross = 4_150_00
    ordered = [slip(1, gross, gross * 13), slip(2, gross, gross * 14), slip(3, gross, gross * 15)]

    assert annualise_payg_income(ordered) == annualise_payg_income(list(reversed(ordered)))


def test_income_result_is_serialisable():
    result = annualise_payg_income([
        slip(1, 4_150_00, 4_150_00 * 13),
        slip(2, 4_150_00, 4_150_00 * 14),
    ])

    assert IncomeResult.model_validate_json(result.model_dump_json()) == result


# --- Repayments -------------------------------------------------------------


def test_amortising_repayment_is_exact_integer_cents():
    repayment = monthly_repayment(500_000_00, Decimal("0.06"), 360)

    assert isinstance(repayment, int)
    assert repayment == 299_775   # $2,997.75


def test_interest_only_repayment_is_principal_times_monthly_rate():
    repayment = monthly_repayment(200_000_00, Decimal("0.06"), 60, interest_only=True)

    assert repayment == 100_000   # $1,000.00


def test_zero_rate_amortises_evenly():
    assert monthly_repayment(120_000_00, Decimal("0"), 120) == 100_000


def test_repayment_rejects_a_float_rate():
    with pytest.raises(TypeError):
        monthly_repayment(500_000_00, 0.06, 360)


def test_annual_repayments_sum_across_splits():
    splits = [
        LoanSplitTerms(split_number=1, amount=600_000_00, product_type="variable", term_months=360),
        LoanSplitTerms(split_number=2, amount=330_000_00, product_type="fixed_3yr", term_months=360),
    ]
    rates = {"variable": Decimal("0.0629"), "fixed_3yr": Decimal("0.0589"), "interest_only": Decimal("0.0684")}

    total = proposed_annual_repayments(splits, rates)
    expected = (
        monthly_repayment(600_000_00, Decimal("0.0629"), 360) * 12
        + monthly_repayment(330_000_00, Decimal("0.0589"), 360) * 12
    )

    assert total == expected
    assert isinstance(total, int)


def test_rate_adjustment_raises_the_repayment():
    splits = [LoanSplitTerms(split_number=1, amount=500_000_00, product_type="variable", term_months=360)]
    rates = {"variable": Decimal("0.0600")}

    assert proposed_annual_repayments(splits, rates, Decimal("0.03")) > proposed_annual_repayments(splits, rates)


def test_missing_rate_for_a_product_raises():
    splits = [LoanSplitTerms(split_number=1, amount=500_000_00, product_type="interest_only", term_months=60)]

    with pytest.raises(KeyError):
        proposed_annual_repayments(splits, {"variable": Decimal("0.06")})


# --- Serviceability ---------------------------------------------------------


def test_serviceability_applies_the_buffer_exactly_once():
    result = calculate_serviceability(
        gross_annual_income=200_000_00,
        existing_commitments=10_000_00,
        proposed_repayments=40_000_00,
        assessment_rate_buffer=Decimal("0.30"),
    )

    assert result.assessed_annual_repayments == 52_000_00
    assert result.total_annual_outgoings == 62_000_00
    assert result.annual_surplus == 138_000_00
    assert result.coverage_ratio == Decimal("3.2258")
    assert isinstance(result.coverage_ratio, Decimal)


def test_serviceability_surplus_may_be_negative():
    result = calculate_serviceability(
        gross_annual_income=50_000_00,
        existing_commitments=10_000_00,
        proposed_repayments=60_000_00,
        assessment_rate_buffer=Decimal("0.30"),
    )

    assert result.annual_surplus == -38_000_00
    assert result.coverage_ratio < Decimal("1")


def test_zero_buffer_leaves_the_repayment_untouched():
    result = calculate_serviceability(100_000_00, 0, 30_000_00, Decimal("0"))

    assert result.assessed_annual_repayments == 30_000_00


def test_serviceability_with_no_outgoings_has_no_coverage_ratio():
    result = calculate_serviceability(100_000_00, 0, 0, Decimal("0.30"))

    assert result.coverage_ratio is None
    assert result.annual_surplus == 100_000_00


def test_serviceability_rejects_float_money_and_float_buffer():
    with pytest.raises(TypeError):
        calculate_serviceability(100_000.0, 0, 0, Decimal("0.30"))
    with pytest.raises(TypeError):
        calculate_serviceability(100_000_00, 0, 0, 0.30)


def test_serviceability_returns_no_verdict():
    """The result carries figures only. Nothing on it says yes or no."""
    result = calculate_serviceability(200_000_00, 10_000_00, 40_000_00, Decimal("0.30"))
    banned = {"approved", "declined", "pass", "fail", "grade", "rating", "recommendation",
              "serviceable", "acceptable"}

    assert banned.isdisjoint(set(result.model_dump().keys()))


# --- Formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    "cents, expected",
    [(0, "$0.00"), (5, "$0.05"), (100, "$1.00"), (585_000_00, "$585,000.00"), (-1_50, "-$1.50")],
)
def test_format_cents(cents, expected):
    assert format_cents(cents) == expected


def test_format_cents_rejects_a_float():
    with pytest.raises(TypeError):
        format_cents(1.5)


def test_format_ratio_as_percent():
    assert format_ratio_as_percent(Decimal("0.7500")) == "75.00%"
    assert format_ratio_as_percent(Decimal("0.8500"), places=1) == "85.0%"


def test_format_ratio_rejects_a_float():
    with pytest.raises(TypeError):
        format_ratio_as_percent(0.75)
