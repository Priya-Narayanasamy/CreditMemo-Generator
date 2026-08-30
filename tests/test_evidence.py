"""Phase 4 tests: the evidence agent.

Covers the three things most likely to go quietly wrong:

- source selection, with known correct answers
- document-to-borrower assignment, which manufactures phantom conflicts when it
  guesses (joint applicants sharing a surname)
- the gap/conflict distinction, end to end against the real data
"""

from __future__ import annotations

from datetime import date

import pytest

from config import DB_PATH, DOCUMENTS_DIR, MAX_ATTEMPTS_PER_FIELD
from src.agents.evidence import (
    EvidenceAgent,
    RuleSourceSelector,
    SourceCandidate,
    compare_values,
    evidence_node,
    normalise_address,
    normalise_name,
)
from src.state import EvidenceItem, MemoState, Provenance
from src.tools import database as db
from src.tools.extraction import LocalTableExtractor
from src.tools.templates import (
    borrower_count,
    field_specs,
    optional_fields,
    required_fields,
    select_template,
)

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)


@pytest.fixture(scope="module")
def agent() -> EvidenceAgent:
    return EvidenceAgent(extractor=LocalTableExtractor())


@pytest.fixture(scope="module")
def runs() -> dict[str, MemoState]:
    """One evidence run per application, shared across the tests."""
    results = {}
    for number in db.list_application_numbers():
        state = MemoState(application_number=number)
        evidence_node(state, EvidenceAgent(extractor=LocalTableExtractor()))
        results[number] = state
    return results


# --- Template selection -----------------------------------------------------


@pytest.mark.parametrize(
    "purpose, structure, expected",
    [
        ("owner_occupied", "single", "owner_occupied_single"),
        ("owner_occupied", "joint", "owner_occupied_joint"),
        ("investment", "single", "investment_single"),
        ("investment", "joint", "investment_joint"),
    ],
)
def test_template_selection(purpose, structure, expected):
    assert select_template(purpose, structure) == expected


def test_an_unknown_combination_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        select_template("bridging", "single")


def test_a_joint_template_requires_a_second_borrower_set():
    single = set(required_fields("owner_occupied_single"))
    joint = set(required_fields("owner_occupied_joint"))

    assert borrower_count("owner_occupied_joint") == 2
    assert joint > single
    assert any(name.startswith("borrower_2_") for name in joint)
    assert not any(name.startswith("borrower_2_") for name in single)


def test_required_and_optional_fields_do_not_overlap():
    for template_id in ("owner_occupied_single", "investment_joint"):
        assert not set(required_fields(template_id)) & set(optional_fields(template_id))


def test_every_field_declares_where_to_look_for_it():
    for spec in field_specs("investment_joint"):
        assert spec.source_kind in {"database", "document", "computed", "verification"}
        if spec.source_kind == "document":
            assert spec.document_type is not None
            assert spec.borrower_position is not None


# --- Source selection -------------------------------------------------------


def candidates() -> list[SourceCandidate]:
    return [
        SourceCandidate(ref="scan_0043.pdf", document_type="equifax", subject_name="Daniel Okonkwo"),
        SourceCandidate(ref="IMG_8871.pdf", document_type="kyc", subject_name="Daniel Okonkwo"),
        SourceCandidate(ref="attachment(3).pdf", document_type="payslip", subject_name="Daniel Okonkwo"),
    ]


@pytest.mark.parametrize(
    "field_name, expected_type, expected_ref",
    [
        ("borrower_1_credit_score", "equifax", "scan_0043.pdf"),
        ("borrower_1_kyc_method", "kyc", "IMG_8871.pdf"),
        ("borrower_1_employer", "payslip", "attachment(3).pdf"),
    ],
)
def test_source_selection_has_known_correct_answers(field_name, expected_type, expected_ref):
    selector = RuleSourceSelector(expected_type, "Daniel Okonkwo")

    assert selector.select(field_name, candidates()).ref == expected_ref


def test_selection_prefers_the_right_subject_over_the_right_type():
    pool = [
        SourceCandidate(ref="other.pdf", document_type="kyc", subject_name="Thomas Raghunathan"),
        SourceCandidate(ref="mine.pdf", document_type="kyc", subject_name="Priyanka Raghunathan"),
    ]
    selector = RuleSourceSelector("kyc", "Priyanka Raghunathan")

    assert selector.select("borrower_1_kyc_method", pool).ref == "mine.pdf"


def test_selection_returns_nothing_when_no_source_could_hold_the_field():
    selector = RuleSourceSelector("payslip", "Daniel Okonkwo")
    pool = [SourceCandidate(ref="a.pdf", document_type="kyc", subject_name="Daniel Okonkwo")]

    assert selector.select("borrower_1_employer", pool) is None


def test_selection_falls_back_to_an_unreadable_document():
    """An unreadable document might be the very one being sought. Trying it is what
    turns a silent gap into a reported parse failure."""
    pool = [SourceCandidate(ref="corrupt.pdf", document_type="unknown", readable=False)]
    selector = RuleSourceSelector("equifax", "Beatrix Lindqvist")

    assert selector.select("borrower_1_credit_score", pool).ref == "corrupt.pdf"


def test_selection_prefers_a_readable_document_over_an_unreadable_one():
    pool = [
        SourceCandidate(ref="corrupt.pdf", document_type="unknown", readable=False),
        SourceCandidate(ref="good.pdf", document_type="equifax", subject_name="X"),
    ]

    assert RuleSourceSelector("equifax", "X").select("borrower_1_credit_score", pool).ref == "good.pdf"


def test_selection_with_no_candidates_returns_nothing():
    assert RuleSourceSelector("kyc", "X").select("borrower_1_full_name", []) is None


# --- Document to borrower assignment ----------------------------------------


def test_an_exact_name_match_outscores_a_shared_surname(agent):
    borrower = db.get_application_file("APP-2026-0002").borrower_at(1)

    exact = agent.match_score({"subject_name": borrower.full_name}, borrower)
    surname_only = agent.match_score({"subject_name": "Thomas Raghunathan"}, borrower)

    assert exact > surname_only


def test_a_misspelled_name_still_matches_on_date_of_birth_and_address(agent):
    """Seeded defect 4. The document must still be recognised as this borrower's,
    or the discrepancy is never found at all."""
    borrower = db.get_application_file("APP-2026-0004").borrower_at(1)
    score = agent.match_score({
        "subject_name": "Katherine Ellingham",
        "subject_date_of_birth": borrower.date_of_birth.isoformat(),
        "subject_address": borrower.residential_address,
    }, borrower)

    assert score >= 60


def test_an_unrelated_document_scores_nothing(agent):
    borrower = db.get_application_file("APP-2026-0001").borrower_at(1)

    assert agent.match_score({"subject_name": "Someone Else"}, borrower) == 0


def test_joint_applicants_do_not_receive_each_others_documents(runs):
    """Two borrowers sharing a surname and an address is the case that manufactures
    phantom conflicts."""
    state = runs["APP-2026-0002"]

    assert state.unresolved == {}
    assert state.ledger["borrower_1_full_name"].value == "Priyanka Raghunathan"
    assert state.ledger["borrower_2_full_name"].value == "Thomas Raghunathan"


def test_each_document_is_attributed_to_one_borrower(runs):
    subjects = {
        record.filename: record.subject_name
        for record in runs["APP-2026-0002"].documents
        if record.subject_name
    }

    assert len(set(subjects.values())) == 2


# --- Comparison -------------------------------------------------------------


def item(value, kind="database"):
    return EvidenceItem(field_name="f", value=value, provenance=Provenance(
        source_kind=kind, detail={"table": "t", "column": "c", "row_key": "1"}))


def test_matching_values_are_a_match():
    assert compare_values("borrower_1_full_name", item("Daniel Okonkwo"),
                          item("Daniel Okonkwo", "document")).status == "match"


def test_differing_values_are_a_conflict():
    assert compare_values("borrower_1_full_name", item("Kathryn Ellingham"),
                          item("Katherine Ellingham", "document")).status == "conflict"


def test_one_side_missing_is_not_present_never_a_conflict():
    """Absence is a gap and disagreement is a conflict, and the two escalate
    differently."""
    assert compare_values("borrower_1_full_name", item("X"), None).status == "not_present"
    assert compare_values("borrower_1_full_name", None, item("X")).status == "not_present"


def test_comparison_ignores_case_and_spacing_but_not_spelling():
    assert compare_values("borrower_1_full_name", item("Daniel  Okonkwo"),
                          item("daniel okonkwo", "document")).status == "match"
    assert compare_values("borrower_1_full_name", item("Daniel Okonkwo"),
                          item("Daniell Okonkwo", "document")).status == "conflict"


def test_address_comparison_tolerates_punctuation():
    assert normalise_address("3/91 Rosslyn Parade, Prospect SA 5082") == \
           normalise_address("3 91 Rosslyn Parade Prospect SA 5082")


def test_dates_compare_by_value():
    assert compare_values("borrower_1_date_of_birth", item(date(1984, 3, 1)),
                          item("1984-03-01", "document")).status == "match"
    assert compare_values("borrower_1_date_of_birth", item(date(1984, 3, 1)),
                          item("1984-01-30", "document")).status == "conflict"


def test_normalise_name_handles_empty_input():
    assert normalise_name("") == ""


# --- End to end on the real data --------------------------------------------


def test_the_clean_application_resolves_every_required_field(runs):
    state = runs["APP-2026-0001"]

    assert state.template_id == "owner_occupied_single"
    assert state.missing_fields() == []
    assert state.unresolved == {}
    assert state.is_complete()


def test_every_ledger_entry_has_a_resolved_provenance(runs):
    for number, state in runs.items():
        for name, entry in state.ledger.items():
            assert entry.provenance.source_kind in {"database", "document", "computed", "analyst"}
            assert entry.provenance.detail, f"{number}/{name} has an empty provenance"
            assert entry.provenance.describe()


def test_money_stays_integer_cents_through_the_ledger(runs):
    state = runs["APP-2026-0001"]

    for name in ("total_loan_amount", "valuation_amount", "borrower_1_payg_income",
                 "computed_total_annual_income", "computed_annual_repayments"):
        assert isinstance(state.ledger[name].value, int), name


def test_ratios_are_stored_as_strings_never_floats(runs):
    """A Decimal survives the ledger as its exact string. A float would not."""
    state = runs["APP-2026-0001"]

    assert state.ledger["computed_lvr"].value == "0.7500"
    assert isinstance(state.ledger["computed_coverage_ratio"].value, str)


def test_income_is_computed_from_the_payslips_that_produced_it(runs):
    provenance = runs["APP-2026-0001"].ledger["borrower_1_payg_income"].provenance

    assert provenance.source_kind == "computed"
    assert provenance.detail["function"] == "annualise_payg_income"
    assert len(provenance.detail["inputs"]) == 3


def test_a_conflict_leaves_no_value_in_the_ledger(runs):
    state = runs["APP-2026-0004"]

    assert "borrower_1_full_name" not in state.ledger
    assert state.unresolved["borrower_1_full_name"].is_conflict


def test_a_conflict_consumes_no_retry_budget(runs):
    entry = runs["APP-2026-0008"].unresolved["borrower_1_date_of_birth"]

    assert entry.is_conflict
    assert entry.attempts == []
    assert not runs["APP-2026-0008"].budget_exhausted(
        "borrower_1_date_of_birth", MAX_ATTEMPTS_PER_FIELD)


def test_a_defect_isolates_to_its_own_field(runs):
    """A defect that halts the whole run rather than isolating to its field is a
    bug. Fields unaffected by the defect must still resolve."""
    state = runs["APP-2026-0004"]

    assert list(state.unresolved) == ["borrower_1_full_name"]
    assert state.ledger["borrower_1_credit_score"].value == 758
    assert state.ledger["borrower_1_payg_income"] is not None
    assert state.ledger["computed_lvr"].value == "0.8000"


def test_a_missing_document_set_does_not_consume_another_fields_budget(runs):
    """Seeded defect 3: the second applicant has no payslips."""
    state = runs["APP-2026-0003"]

    assert "borrower_2_payg_income" in state.unresolved
    assert state.unresolved["borrower_2_payg_income"].reason == "not_found"
    assert "borrower_1_payg_income" in state.ledger
    assert state.attempts_for("borrower_1_payg_income") == 0


def test_an_unreadable_document_is_reported_as_a_parse_failure_not_a_silent_gap(runs):
    """Seeded defect 7. The corrupt document must be named in the escalation."""
    state = runs["APP-2026-0007"]
    entry = state.unresolved["borrower_1_credit_score"]

    assert entry.reason == "parse_failed"
    assert any(a.outcome == "parse_failed" for a in entry.attempts)
    assert any(a.source_ref.endswith(".pdf") for a in entry.attempts)
    assert "could not be read" in entry.describe()


def test_inconsistent_payslips_produce_a_conflict_not_an_averaged_figure(runs):
    """Seeded defect 5."""
    state = runs["APP-2026-0005"]
    entry = state.unresolved["borrower_1_payg_income"]

    assert entry.is_conflict
    assert "borrower_1_payg_income" not in state.ledger
    assert len(entry.conflicting_values) >= 1
    assert "Year-to-date" in entry.conflicting_values[0].value


def test_a_stated_lvr_disagreement_is_a_conflict(runs):
    """Seeded defect 6. Two sources for one fact, so the agent does not pick one."""
    state = runs["APP-2026-0006"]
    entry = state.unresolved["computed_lvr"]

    assert entry.is_conflict
    assert "computed_lvr" not in state.ledger
    assert {v.value for v in entry.conflicting_values} == {"0.8500", "0.7800"}
    assert {v.provenance.source_kind for v in entry.conflicting_values} == {"computed", "database"}


def test_serviceability_is_not_computed_from_a_partial_income_picture(runs):
    state = runs["APP-2026-0003"]

    assert "computed_coverage_ratio" not in state.ledger
    assert "partial income picture" in state.unresolved["computed_coverage_ratio"].describe()


def test_the_escalation_names_the_field_the_sources_and_the_outcomes(runs):
    escalation = EvidenceAgent(extractor=LocalTableExtractor()).build_escalation(
        runs["APP-2026-0004"])

    assert escalation.fields == ["borrower_1_full_name"]
    assert "Kathryn Ellingham" in escalation.detail
    assert "Katherine Ellingham" in escalation.detail
    assert "Nothing has been written" in escalation.detail


def test_a_clean_application_raises_no_escalation(runs, agent):
    for number in ("APP-2026-0001", "APP-2026-0002"):
        assert agent.build_escalation(runs[number]) is None


def test_a_missing_application_escalates_rather_than_crashing(agent):
    state = MemoState(application_number="APP-9999-9999")
    result = evidence_node(state, agent)

    assert result["escalation"] is not None
    assert "does not exist" in result["escalation"].detail


def test_the_run_is_deterministic():
    ledgers = []
    for _ in range(3):
        state = MemoState(application_number="APP-2026-0001")
        evidence_node(state, EvidenceAgent(extractor=LocalTableExtractor()))
        ledgers.append({name: str(item.value) for name, item in sorted(state.ledger.items())})

    assert all(ledger == ledgers[0] for ledger in ledgers)


def test_no_figure_exists_outside_the_ledger(runs):
    """Every value the memo may state is in the ledger, with a source."""
    state = runs["APP-2026-0001"]

    assert set(state.figures()) == set(state.ledger)
    assert all(name in state.ledger for name in state.required_fields)
