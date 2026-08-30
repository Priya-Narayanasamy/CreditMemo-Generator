"""Pure calculation. No model call reaches this module, ever.

Currency is integer cents throughout. Ratios are `Decimal`. There is no float in
any signature or return value here, and none in any intermediate step that touches
money - `Decimal` division is used and results are quantised explicitly.

Nothing here returns a judgement. `annualise_payg_income` returns a conflict when
payslip year-to-date figures do not reconcile; it does not average them, pick the
larger, or decide which slip to believe.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel

FORTNIGHTS_PER_YEAR = 26
MONTHS_PER_YEAR = 12

RATIO_PLACES = Decimal("0.0001")


def _ratio(numerator: int, denominator: int) -> Decimal:
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RATIO_PLACES, rounding=ROUND_HALF_UP
    )


def _cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --- LVR --------------------------------------------------------------------


def calculate_lvr(total_loan_amount: int, valuation_amount: int) -> Decimal:
    """Loan to valuation ratio, to four decimal places.

    Both arguments are integer cents.
    """
    if not isinstance(total_loan_amount, int) or not isinstance(valuation_amount, int):
        raise TypeError("LVR inputs must be integer cents")
    if valuation_amount <= 0:
        raise ValueError("valuation_amount must be positive")
    if total_loan_amount < 0:
        raise ValueError("total_loan_amount must not be negative")

    return _ratio(total_loan_amount, valuation_amount)


# --- PAYG income ------------------------------------------------------------


class PayslipFigures(BaseModel):
    """The figures one payslip contributes. Sourced, never invented."""

    source_filename: str
    period_start: date
    period_end: date
    gross_for_period: int          # cents
    ytd_gross: int | None = None   # cents
    pay_frequency: Literal["fortnightly", "monthly", "weekly"] = "fortnightly"


class IncomeConflict(BaseModel):
    kind: Literal["ytd_not_sequential", "gross_varies", "frequency_varies"]
    description: str
    sources: list[str]


class IncomeResult(BaseModel):
    status: Literal["resolved", "conflict", "insufficient_data"]
    annual_gross: int | None = None      # cents
    period_gross: int | None = None      # cents
    periods_per_year: int | None = None
    source_filenames: list[str] = []
    conflicts: list[IncomeConflict] = []
    detail: str = ""


PERIODS_PER_YEAR = {"fortnightly": 26, "monthly": 12, "weekly": 52}


def annualise_payg_income(payslips: list[PayslipFigures]) -> IncomeResult:
    """Annualise PAYG income from a set of payslips.

    Returns a conflict rather than a figure when the slips do not agree with each
    other. Specifically:

    - the year-to-date gross figures must advance by exactly the period gross
      between consecutive slips
    - the period gross must be constant across the slips
    - the pay frequency must be constant across the slips

    Inconsistent year-to-date figures are never averaged or reconciled. Two sources
    disagreeing is a conflict for the analyst, not an arithmetic problem to smooth
    over.
    """
    if len(payslips) < 2:
        return IncomeResult(
            status="insufficient_data",
            source_filenames=[p.source_filename for p in payslips],
            detail=(
                f"{len(payslips)} payslip(s) available; at least 2 are needed to "
                "annualise income and to cross-check year-to-date figures."
            ),
        )

    ordered = sorted(payslips, key=lambda p: p.period_end)
    sources = [p.source_filename for p in ordered]
    conflicts: list[IncomeConflict] = []

    frequencies = {p.pay_frequency for p in ordered}
    if len(frequencies) > 1:
        conflicts.append(IncomeConflict(
            kind="frequency_varies",
            description=(
                "Payslips state different pay frequencies: "
                + ", ".join(sorted(frequencies))
            ),
            sources=sources,
        ))

    gross_values = {p.gross_for_period for p in ordered}
    if len(gross_values) > 1:
        conflicts.append(IncomeConflict(
            kind="gross_varies",
            description=(
                "Gross for period differs across the payslips: "
                + ", ".join(f"{v / 100:,.2f}" for v in sorted(gross_values))
            ),
            sources=sources,
        ))

    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.ytd_gross is None or later.ytd_gross is None:
            continue
        movement = later.ytd_gross - earlier.ytd_gross
        if movement != later.gross_for_period:
            conflicts.append(IncomeConflict(
                kind="ytd_not_sequential",
                description=(
                    f"Year-to-date gross moves by {movement / 100:,.2f} between the "
                    f"periods ending {earlier.period_end.isoformat()} and "
                    f"{later.period_end.isoformat()}, but the later slip states a "
                    f"period gross of {later.gross_for_period / 100:,.2f}."
                ),
                sources=[earlier.source_filename, later.source_filename],
            ))

    if conflicts:
        return IncomeResult(
            status="conflict",
            source_filenames=sources,
            conflicts=conflicts,
            detail=(
                "Payslip figures do not reconcile. They have not been averaged or "
                "otherwise combined."
            ),
        )

    period_gross = ordered[0].gross_for_period
    periods = PERIODS_PER_YEAR[ordered[0].pay_frequency]

    return IncomeResult(
        status="resolved",
        annual_gross=period_gross * periods,
        period_gross=period_gross,
        periods_per_year=periods,
        source_filenames=sources,
        detail=(
            f"{period_gross / 100:,.2f} per {ordered[0].pay_frequency[:-2]}y period "
            f"across {len(ordered)} consistent payslips, annualised over {periods} periods."
        ),
    )


# --- Repayments -------------------------------------------------------------


def monthly_repayment(
    principal: int,
    annual_rate: Decimal,
    term_months: int,
    interest_only: bool = False,
) -> int:
    """Scheduled monthly repayment in integer cents.

    `annual_rate` is a nominal annual rate as a Decimal, e.g. `Decimal("0.0629")`.
    """
    if not isinstance(principal, int):
        raise TypeError("principal must be integer cents")
    if not isinstance(annual_rate, Decimal):
        raise TypeError("annual_rate must be a Decimal")
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if principal < 0:
        raise ValueError("principal must not be negative")

    monthly_rate = annual_rate / Decimal(MONTHS_PER_YEAR)

    if interest_only:
        return _cents(Decimal(principal) * monthly_rate)

    if monthly_rate == 0:
        return _cents(Decimal(principal) / Decimal(term_months))

    growth = (Decimal(1) + monthly_rate) ** term_months
    return _cents(Decimal(principal) * monthly_rate * growth / (growth - Decimal(1)))


class LoanSplitTerms(BaseModel):
    split_number: int
    amount: int                    # cents
    product_type: Literal["variable", "fixed_3yr", "interest_only"]
    term_months: int


def proposed_annual_repayments(
    splits: list[LoanSplitTerms],
    rates: dict[str, Decimal],
    rate_adjustment: Decimal = Decimal("0"),
) -> int:
    """Total annual scheduled repayments across every split, in integer cents.

    `rate_adjustment` is added to each product's rate before the repayment is
    computed - this is where an assessment buffer belongs. Pass zero for note-rate
    repayments.
    """
    total = 0
    for split in splits:
        if split.product_type not in rates:
            raise KeyError(f"no rate configured for product type {split.product_type!r}")
        rate = rates[split.product_type] + rate_adjustment
        total += monthly_repayment(
            split.amount,
            rate,
            split.term_months,
            interest_only=split.product_type == "interest_only",
        ) * MONTHS_PER_YEAR
    return total


# --- Serviceability ---------------------------------------------------------


class ServiceabilityResult(BaseModel):
    """Arithmetic only. Whether the surplus is adequate is the analyst's call.

    `assessed_annual_repayments` is the proposed repayment uplifted by the
    assessment buffer. The buffer is applied here and nowhere else, so it cannot be
    counted twice.
    """

    gross_annual_income: int              # cents
    existing_commitments: int             # cents
    proposed_annual_repayments: int       # cents, at the note rate
    assessment_rate_buffer: Decimal
    assessed_annual_repayments: int       # cents, uplifted by the buffer
    total_annual_outgoings: int           # cents
    annual_surplus: int                   # cents, may be negative
    coverage_ratio: Decimal | None        # income / outgoings, None when outgoings are nil


def calculate_serviceability(
    gross_annual_income: int,
    existing_commitments: int,
    proposed_repayments: int,
    assessment_rate_buffer: Decimal,
) -> ServiceabilityResult:
    """Income against outgoings, with the proposed repayment uplifted by a buffer.

    All money arguments are annual, in integer cents. `proposed_repayments` must be
    at the note rate - the buffer is applied here. `assessment_rate_buffer` is a
    proportional uplift expressed as a Decimal, e.g. `Decimal("0.30")` for a 30%
    uplift on the scheduled repayment.

    Returns figures, not a conclusion. Nothing in this function decides whether the
    borrower can service the loan.
    """
    for name, value in (
        ("gross_annual_income", gross_annual_income),
        ("existing_commitments", existing_commitments),
        ("proposed_repayments", proposed_repayments),
    ):
        if not isinstance(value, int):
            raise TypeError(f"{name} must be integer cents")
        if value < 0:
            raise ValueError(f"{name} must not be negative")

    if not isinstance(assessment_rate_buffer, Decimal):
        raise TypeError("assessment_rate_buffer must be a Decimal")
    if assessment_rate_buffer < 0:
        raise ValueError("assessment_rate_buffer must not be negative")

    assessed = _cents(Decimal(proposed_repayments) * (Decimal(1) + assessment_rate_buffer))
    outgoings = existing_commitments + assessed

    return ServiceabilityResult(
        gross_annual_income=gross_annual_income,
        existing_commitments=existing_commitments,
        proposed_annual_repayments=proposed_repayments,
        assessment_rate_buffer=assessment_rate_buffer,
        assessed_annual_repayments=assessed,
        total_annual_outgoings=outgoings,
        annual_surplus=gross_annual_income - outgoings,
        coverage_ratio=_ratio(gross_annual_income, outgoings) if outgoings > 0 else None,
    )


# --- Small helpers used by the templates and the ledger ---------------------


def sum_cents(values: list[int]) -> int:
    for value in values:
        if not isinstance(value, int):
            raise TypeError("all values must be integer cents")
    return sum(values)


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days


def format_cents(cents: int) -> str:
    """Render integer cents as currency. The only place cents become a string."""
    if not isinstance(cents, int):
        raise TypeError("format_cents expects integer cents")
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def format_ratio_as_percent(ratio: Decimal, places: int = 2) -> str:
    if not isinstance(ratio, Decimal):
        raise TypeError("format_ratio_as_percent expects a Decimal")
    quantum = Decimal(1).scaleb(-places)
    return f"{(ratio * 100).quantize(quantum, rounding=ROUND_HALF_UP)}%"
