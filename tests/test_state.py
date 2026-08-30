"""Phase 2 tests: the evidence ledger.

Two rules carry most of the weight here:

- retry budget is per field, so exhausting one field's attempts leaves every other
  field's budget untouched
- a conflict is not a gap; it bypasses the retry budget entirely, because retrying
  cannot resolve two sources that disagree
"""

from __future__ import annotations

import pytest

from config import MAX_ATTEMPTS_PER_FIELD
from src.state import (
    ApprovalRequest,
    Attempt,
    Escalation,
    EvidenceItem,
    MemoState,
    Provenance,
)


def db_provenance(table="applications", column="total_loan_amount", row="APP-2026-0001"):
    return Provenance(source_kind="database", detail={"table": table, "column": column, "row_key": row})


def doc_provenance(filename="scan_0043.pdf", page=1, document_type="payslip"):
    return Provenance(
        source_kind="document",
        detail={"filename": filename, "page": page, "document_type": document_type},
    )


def state(**kwargs) -> MemoState:
    return MemoState(application_number="APP-2026-0001", **kwargs)


# --- Provenance -------------------------------------------------------------


def test_every_provenance_kind_describes_itself():
    assert "applications.total_loan_amount" in db_provenance().describe()
    assert "scan_0043.pdf" in doc_provenance().describe()

    computed = Provenance(
        source_kind="computed",
        detail={"function": "calculate_lvr", "inputs": ["total_loan_amount", "valuation_amount"]},
    )
    assert "calculate_lvr" in computed.describe()
    assert "valuation_amount" in computed.describe()

    analyst = Provenance(source_kind="analyst", detail={"supplied_by": "P. Narayanasamy"})
    assert "P. Narayanasamy" in analyst.describe()


def test_provenance_records_when_it_was_retrieved():
    assert db_provenance().retrieved_at is not None


# --- Recording --------------------------------------------------------------


def test_recording_a_value_clears_the_gap():
    s = state(required_fields=["total_loan_amount"])
    s.note_attempt("total_loan_amount", Attempt(
        source_kind="document", source_ref="scan_0043.pdf", outcome="not_present"))

    assert "total_loan_amount" in s.unresolved

    s.record("total_loan_amount", 585_000_00, db_provenance())

    assert s.unresolved == {}
    assert s.ledger["total_loan_amount"].value == 585_000_00
    assert s.is_complete()


def test_missing_fields_lists_only_required_ones_not_in_the_ledger():
    s = state(required_fields=["a", "b"], optional_fields=["c"])
    s.record("a", 1, db_provenance())

    assert s.missing_fields() == ["b"]


def test_figures_exposes_exactly_the_ledger():
    s = state()
    s.record("computed_lvr", "0.7500", db_provenance())

    assert s.figures() == {"computed_lvr": "0.7500"}


# --- Per-field retry budget -------------------------------------------------


def test_retry_budget_is_tracked_per_field_not_globally():
    s = state(required_fields=["field_a", "field_b"])

    for index in range(MAX_ATTEMPTS_PER_FIELD):
        s.note_attempt("field_a", Attempt(
            source_kind="document", source_ref=f"doc{index}.pdf", outcome="parse_failed"))

    assert s.budget_exhausted("field_a", MAX_ATTEMPTS_PER_FIELD)
    assert not s.budget_exhausted("field_b", MAX_ATTEMPTS_PER_FIELD)
    assert s.attempts_for("field_b") == 0


def test_budget_is_not_exhausted_before_the_limit():
    s = state()
    s.note_attempt("field_a", Attempt(
        source_kind="document", source_ref="a.pdf", outcome="parse_failed"))

    assert not s.budget_exhausted("field_a", MAX_ATTEMPTS_PER_FIELD)


def test_a_field_never_attempted_has_not_exhausted_its_budget():
    assert not state().budget_exhausted("never_tried", MAX_ATTEMPTS_PER_FIELD)


# --- Attempt outcomes -------------------------------------------------------


def test_a_clean_parse_without_the_field_is_not_retryable():
    """A document that parsed cleanly and did not contain the field must never be
    retried."""
    attempt = Attempt(source_kind="document", source_ref="a.pdf", outcome="not_present")

    assert not attempt.retryable


def test_a_parse_failure_is_retryable():
    attempt = Attempt(source_kind="document", source_ref="a.pdf", outcome="parse_failed")

    assert attempt.retryable


def test_exhausted_sources_excludes_the_retryable_ones():
    s = state()
    s.note_attempt("field_a", Attempt(source_kind="document", source_ref="clean.pdf", outcome="not_present"))
    s.note_attempt("field_a", Attempt(source_kind="document", source_ref="broken.pdf", outcome="parse_failed"))

    entry = s.unresolved["field_a"]

    assert entry.tried_sources() == {"clean.pdf", "broken.pdf"}
    assert entry.exhausted_sources() == {"clean.pdf"}


def test_the_reason_follows_the_most_recent_meaningful_outcome():
    s = state()
    s.note_attempt("f", Attempt(source_kind="document", source_ref="a.pdf", outcome="not_present"))

    assert s.unresolved["f"].reason == "not_found"

    s.note_attempt("f", Attempt(source_kind="document", source_ref="b.pdf", outcome="parse_failed"))

    assert s.unresolved["f"].reason == "parse_failed"


# --- Conflicts --------------------------------------------------------------


def test_a_conflict_does_not_consume_retry_budget():
    s = state()
    s.record_conflict("borrower_1_full_name", [
        EvidenceItem(field_name="borrower_1_full_name", value="Kathryn Ellingham",
                     provenance=db_provenance("borrowers", "full_name", "1")),
        EvidenceItem(field_name="borrower_1_full_name", value="Katherine Ellingham",
                     provenance=doc_provenance("IMG_8871.pdf", 1, "kyc")),
    ])

    entry = s.unresolved["borrower_1_full_name"]

    assert entry.is_conflict
    assert entry.attempts == []
    assert not s.budget_exhausted("borrower_1_full_name", MAX_ATTEMPTS_PER_FIELD)


def test_a_conflict_never_degrades_back_into_a_gap():
    s = state()
    s.record_conflict("f", [
        EvidenceItem(field_name="f", value="a", provenance=db_provenance()),
        EvidenceItem(field_name="f", value="b", provenance=doc_provenance()),
    ])
    s.note_attempt("f", Attempt(source_kind="document", source_ref="c.pdf", outcome="not_present"))

    entry = s.unresolved["f"]

    assert entry.reason == "conflict"
    assert entry.attempts == [], "a conflicting field must not accrue retry attempts"


def test_a_conflicting_field_is_removed_from_the_ledger():
    """A field two sources disagree about must not sit in the ledger looking
    resolved - no figure may reach the memo that way."""
    s = state()
    s.record("f", "Kathryn Ellingham", db_provenance())
    s.record_conflict("f", [
        EvidenceItem(field_name="f", value="Kathryn Ellingham", provenance=db_provenance()),
        EvidenceItem(field_name="f", value="Katherine Ellingham", provenance=doc_provenance()),
    ])

    assert "f" not in s.ledger
    assert not s.is_complete()


def test_conflicts_are_listed_separately_from_gaps():
    s = state()
    s.note_attempt("gap_field", Attempt(source_kind="document", source_ref="a.pdf", outcome="not_present"))
    s.record_conflict("conflict_field", [
        EvidenceItem(field_name="conflict_field", value=1, provenance=db_provenance()),
        EvidenceItem(field_name="conflict_field", value=2, provenance=doc_provenance()),
    ])

    assert [c.field_name for c in s.conflicts()] == ["conflict_field"]


def test_both_conflicting_values_keep_their_own_provenance():
    s = state()
    s.record_conflict("borrower_1_date_of_birth", [
        EvidenceItem(field_name="borrower_1_date_of_birth", value="1984-03-01",
                     provenance=db_provenance("borrowers", "date_of_birth", "12")),
        EvidenceItem(field_name="borrower_1_date_of_birth", value="1984-01-30",
                     provenance=doc_provenance("scan_0043.pdf", 1, "equifax")),
    ])
    values = s.unresolved["borrower_1_date_of_birth"].conflicting_values

    assert len(values) == 2
    assert values[0].provenance.source_kind == "database"
    assert values[1].provenance.source_kind == "document"
    assert values[0].provenance.describe() != values[1].provenance.describe()


# --- Escalation detail ------------------------------------------------------


def test_an_escalation_states_what_was_tried_and_what_each_source_returned():
    """"Extraction failed" is not an acceptable escalation."""
    s = state()
    s.note_attempt("borrower_2_payg_income", Attempt(
        source_kind="document", source_ref="scan_0044.pdf", outcome="not_present",
        detail="classified as a credit report, contains no payslip fields"))
    s.note_attempt("borrower_2_payg_income", Attempt(
        source_kind="document", source_ref="IMG_0117.pdf", outcome="not_present",
        detail="classified as an identity record, contains no payslip fields"))

    described = s.unresolved["borrower_2_payg_income"].describe()

    assert "borrower_2_payg_income" in described
    assert "scan_0044.pdf" in described and "IMG_0117.pdf" in described
    assert "not_present" in described
    assert "credit report" in described


def test_a_conflict_escalation_shows_both_values_and_both_sources():
    s = state()
    s.record_conflict("borrower_1_full_name", [
        EvidenceItem(field_name="borrower_1_full_name", value="Kathryn Ellingham",
                     provenance=db_provenance("borrowers", "full_name", "6")),
        EvidenceItem(field_name="borrower_1_full_name", value="Katherine Ellingham",
                     provenance=doc_provenance("IMG_8871.pdf", 1, "kyc")),
    ])
    described = s.unresolved["borrower_1_full_name"].describe()

    assert "Kathryn Ellingham" in described and "Katherine Ellingham" in described
    assert "borrowers.full_name" in described
    assert "IMG_8871.pdf" in described


# --- Interrupt types --------------------------------------------------------


def test_the_two_interrupt_types_are_separate_fields():
    s = state()
    s.escalation = Escalation(fields=["f"], summary="cannot proceed", detail="...")

    assert s.escalation.interrupt_type == "ESCALATION"
    assert s.approval_request is None

    s.escalation = None
    s.approval_request = ApprovalRequest(summary="ready", figure_count=12, finding_count=3)

    assert s.approval_request.interrupt_type == "APPROVAL"
    assert s.escalation is None


# --- Serialisation ----------------------------------------------------------


def test_state_round_trips_through_json():
    """The checkpointer stores state between interrupts, so it must serialise."""
    s = state(required_fields=["a"], template_id="owner_occupied_single")
    s.record("a", 585_000_00, db_provenance())
    s.record_conflict("b", [
        EvidenceItem(field_name="b", value=1, provenance=db_provenance()),
        EvidenceItem(field_name="b", value=2, provenance=doc_provenance()),
    ])
    s.approved_memo = None

    restored = MemoState.model_validate_json(s.model_dump_json())

    assert restored.ledger["a"].value == 585_000_00
    assert restored.unresolved["b"].is_conflict
    assert restored.template_id == "owner_occupied_single"


def test_an_approved_memo_survives_a_round_trip():
    s = state()
    s.approved_memo = "# Credit memo\n"
    s.approved_by = "P. Narayanasamy"

    restored = MemoState.model_validate_json(s.model_dump_json())

    assert restored.approved_memo == "# Credit memo\n"
    assert restored.approved_by == "P. Narayanasamy"


@pytest.mark.parametrize("field", ["ledger", "unresolved", "policy_findings", "review_notes"])
def test_collections_default_empty_not_shared(field):
    first, second = state(), state()

    assert getattr(first, field) is not getattr(second, field)
