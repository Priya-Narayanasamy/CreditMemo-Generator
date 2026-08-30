"""Phase 9: one end-to-end test per seeded defect.

Every expectation is read from `data/defects.json`, never hard-coded here. Adding
a defect to the data set adds a case to the parametrised tests automatically.

Each defect is checked three ways:

- the run halts at the expected interrupt type
- the affected field appears in `state.unresolved` with the expected reason
- fields unaffected by the defect still resolved. A defect that halts the whole
  run rather than isolating to its field is a bug.
"""

from __future__ import annotations

import json

import pytest

from config import DB_PATH, DEFECTS_PATH, DOCUMENTS_DIR, MAX_ATTEMPTS_PER_FIELD
from src.graph import approve, interrupt_type, resolve_escalation, run
from src.state import MemoState

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)

ALL_DEFECTS = json.loads(DEFECTS_PATH.read_text(encoding="utf-8"))["defects"]
DEFECT_IDS = [d["application_number"] for d in ALL_DEFECTS]


@pytest.fixture(scope="module")
def outcomes() -> dict[str, MemoState]:
    """One run per application, shared across the parametrised cases."""
    from tests.conftest import build_offline_graph

    return {
        defect["application_number"]: run(defect["application_number"], build_offline_graph())
        for defect in ALL_DEFECTS
    }


def defect_for(number: str) -> dict:
    return next(d for d in ALL_DEFECTS if d["application_number"] == number)


# --- The declared outcome ---------------------------------------------------


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_the_run_halts_at_the_expected_interrupt(number, outcomes):
    defect = defect_for(number)
    state = outcomes[number]
    expected = "ESCALATION" if defect["expected_outcome"] == "escalation" else "APPROVAL"

    assert interrupt_type(state) == expected, defect["description"]


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_the_affected_field_is_unresolved_for_the_declared_reason(number, outcomes):
    defect = defect_for(number)
    state = outcomes[number]

    for field_name in defect["affected_fields"]:
        assert field_name in state.unresolved, f"{field_name} resolved despite the defect"
        assert state.unresolved[field_name].reason == defect["expected_reason"], (
            f"{field_name} is {state.unresolved[field_name].reason}, "
            f"expected {defect['expected_reason']}"
        )


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_no_memo_is_written_for_a_defective_application(number, outcomes, tmp_path):
    defect = defect_for(number)
    if defect["expected_outcome"] != "escalation":
        pytest.skip("this application is clean")

    state = outcomes[number]

    assert state.rendered_path is None
    assert state.approved_memo is None
    assert state.approval_request is None


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_an_affected_field_never_reaches_the_ledger(number, outcomes):
    state = outcomes[number]

    for field_name in defect_for(number)["affected_fields"]:
        assert field_name not in state.ledger


# --- Isolation: the negative assertion --------------------------------------


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_unaffected_fields_still_resolved(number, outcomes):
    """A defect must isolate to its own field. Most of the file should still come
    through - the analyst needs everything that is not in dispute."""
    state = outcomes[number]

    resolved = len(state.ledger)
    required = len(state.required_fields)

    assert resolved >= required * 0.6, (
        f"{number}: only {resolved} fields resolved against {required} required; "
        f"the defect did not isolate"
    )
    for name in ("total_loan_amount", "valuation_amount", "security_address",
                 "borrower_1_record_name"):
        assert name in state.ledger, f"{number}: {name} should be unaffected"


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_one_defect_does_not_consume_another_fields_retry_budget(number, outcomes):
    state = outcomes[number]
    affected = set(defect_for(number)["affected_fields"])

    for field_name in state.required_fields:
        if field_name in affected or field_name in state.unresolved:
            continue
        assert state.attempts_for(field_name) == 0, (
            f"{number}: {field_name} accrued attempts although it is not affected"
        )


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_conflicts_consume_no_retry_budget_at_all(number, outcomes):
    for entry in outcomes[number].conflicts():
        assert entry.attempts == []
        assert not outcomes[number].budget_exhausted(entry.field_name, MAX_ATTEMPTS_PER_FIELD)


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_gap_attempts_stay_within_the_per_field_budget(number, outcomes):
    for name, entry in outcomes[number].unresolved.items():
        if entry.is_conflict:
            continue
        assert len(entry.attempts) <= MAX_ATTEMPTS_PER_FIELD, name


# --- The escalation message -------------------------------------------------


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_the_escalation_is_actionable(number, outcomes):
    """"Extraction failed" is not an acceptable escalation. It must state which
    field, which sources were tried, and what each returned."""
    defect = defect_for(number)
    if defect["expected_outcome"] != "escalation":
        pytest.skip("this application is clean")

    escalation = outcomes[number].escalation
    detail = escalation.detail

    for field_name in defect["affected_fields"]:
        assert field_name in escalation.fields
        assert field_name in detail

    assert "Nothing has been written" in detail
    assert len(detail) > 120, "the escalation is too terse to act on"


@pytest.mark.parametrize("number", DEFECT_IDS)
def test_a_conflict_escalation_carries_both_values_with_their_own_provenance(number, outcomes):
    defect = defect_for(number)
    if defect["expected_reason"] != "conflict":
        pytest.skip("not a conflict defect")

    for field_name in defect["affected_fields"]:
        entry = outcomes[number].unresolved[field_name]
        values = entry.conflicting_values

        assert len(values) >= 2 or entry.field_name.endswith("_payg_income")
        sources = {value.provenance.describe() for value in values}
        assert len(sources) == len(values), "each value must carry its own provenance"
        for value in values:
            assert value.provenance.detail


# --- Defect-specific behaviour ----------------------------------------------


def test_the_missing_payslips_defect_names_the_applicant(outcomes):
    defect = next(d for d in ALL_DEFECTS if d["category"] == "missing")
    entry = outcomes[defect["application_number"]].unresolved[defect["affected_fields"][0]]

    assert "payslip" in entry.describe().lower()


def test_the_unreadable_defect_names_the_file_and_the_parse_error(outcomes):
    defect = next(d for d in ALL_DEFECTS if d["category"] == "unreadable")
    entry = outcomes[defect["application_number"]].unresolved[defect["affected_fields"][0]]
    described = entry.describe()

    assert ".pdf" in described
    assert "could not be read" in described
    assert any(attempt.outcome == "parse_failed" for attempt in entry.attempts)


def test_the_inconsistent_ytd_defect_produces_no_averaged_figure(outcomes):
    defect = next(d for d in ALL_DEFECTS if d["category"] == "internally_inconsistent")
    state = outcomes[defect["application_number"]]
    field_name = defect["affected_fields"][0]

    assert field_name not in state.ledger
    assert "computed_total_annual_income" not in state.ledger
    assert "Year-to-date" in state.unresolved[field_name].conflicting_values[0].value


def test_the_name_mismatch_defect_preserves_both_spellings(outcomes):
    state = outcomes["APP-2026-0004"]
    values = {v.value for v in state.unresolved["borrower_1_full_name"].conflicting_values}

    assert values == {"Kathryn Ellingham", "Katherine Ellingham"}


def test_the_stated_lvr_defect_reports_both_ratios(outcomes):
    state = outcomes["APP-2026-0006"]
    values = {v.value for v in state.unresolved["computed_lvr"].conflicting_values}

    assert values == {"0.8500", "0.7800"}


def test_the_dob_mismatch_defect_does_not_block_the_rest_of_the_file(outcomes):
    state = outcomes["APP-2026-0008"]

    assert list(state.unresolved) == ["borrower_1_date_of_birth"]
    assert "borrower_1_credit_score" in state.ledger
    assert "borrower_1_payg_income" in state.ledger


# --- The clean applications -------------------------------------------------


@pytest.mark.parametrize(
    "number", [d["application_number"] for d in ALL_DEFECTS if d["category"] == "clean"]
)
def test_a_clean_application_completes_and_can_be_approved(number, outcomes):
    state = outcomes[number]

    assert state.unresolved == {}
    assert state.missing_fields() == []
    assert interrupt_type(state) == "APPROVAL"

    approved = approve(state, "P. Narayanasamy")

    assert approved.approved_memo.startswith("# Credit memo")


@pytest.mark.parametrize(
    "number", [d["application_number"] for d in ALL_DEFECTS if d["category"] == "clean"]
)
def test_a_clean_application_raises_no_discrepancy(number, outcomes):
    discrepancies = [
        f for f in outcomes[number].policy_findings if f.finding_type == "discrepancy"
    ]

    assert discrepancies == []


# --- Recovery ---------------------------------------------------------------


def test_every_escalated_application_can_be_completed_by_an_analyst(outcomes):
    """The escalation is a stopping point, not a dead end. Supplying the disputed
    or missing values must carry each run through to an approval request."""
    from tests.conftest import build_offline_graph

    placeholders = {
        "borrower_1_full_name": "Kathryn Ellingham",
        "borrower_1_date_of_birth": "1984-03-01",
        "borrower_1_residential_address": "88 Napier Street, Bendigo VIC 3550",
        "computed_lvr": "0.8500",
        "borrower_1_credit_score": 861,
        "borrower_1_credit_score_band": "Excellent",
        "borrower_1_credit_enquiries_6m": 1,
        "borrower_1_credit_defaults_count": 0,
        "borrower_1_credit_report_date": "2026-02-02",
        "borrower_1_payg_income": 161_200_00,
        "borrower_2_payg_income": 105_300_00,
        "borrower_2_employer": "Tarragon Facilities Management Pty Ltd",
        "computed_total_annual_income": 266_500_00,
        "computed_annual_repayments": 90_000_00,
        "computed_assessed_repayments": 117_000_00,
        "computed_annual_surplus": 149_500_00,
        "computed_coverage_ratio": "2.2778",
    }

    for defect in ALL_DEFECTS:
        if defect["expected_outcome"] != "escalation":
            continue

        number = defect["application_number"]
        state = outcomes[number].model_copy(deep=True)

        for field_name in list(state.unresolved):
            if field_name not in state.required_fields:
                continue
            assert field_name in placeholders, (
                f"{number}: no analyst value in this test for {field_name}"
            )
            state = resolve_escalation(
                state, field_name, placeholders[field_name], "P. Narayanasamy",
                "supplied during testing",
            )

        state = MemoState.model_validate(build_offline_graph().build().invoke(state))

        assert interrupt_type(state) == "APPROVAL", f"{number} did not recover"
        assert state.escalation is None
