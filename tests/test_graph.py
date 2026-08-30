"""Phase 6 tests: graph assembly and the two interrupt types.

The interrupts mean opposite things and must never be collapsed into one flag. An
escalation says the agent could not proceed; an approval says it finished and
wants permission to write. The tests that matter most here are the ones asserting
that nothing is written before an approval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import DB_PATH, DOCUMENTS_DIR
from src.graph import (
    approve,
    interrupt_type,
    reject,
    resolve_escalation,
    run,
)
from src.state import MemoState

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)

CLEAN = "APP-2026-0001"
CONFLICTED = "APP-2026-0004"


# --- Routing ----------------------------------------------------------------


def test_a_clean_application_reaches_the_approval_interrupt(offline_graph):
    state = run(CLEAN, offline_graph)

    assert interrupt_type(state) == "APPROVAL"
    assert state.approval_request is not None
    assert state.escalation is None


def test_a_conflicted_application_reaches_the_escalation_interrupt(offline_graph):
    state = run(CONFLICTED, offline_graph)

    assert interrupt_type(state) == "ESCALATION"
    assert state.escalation is not None
    assert state.approval_request is None


def test_the_two_interrupts_are_separate_fields_in_state(offline_graph):
    escalated = run(CONFLICTED, offline_graph)
    approved = run(CLEAN, offline_graph)

    assert escalated.escalation.interrupt_type == "ESCALATION"
    assert approved.approval_request.interrupt_type == "APPROVAL"
    assert escalated.approval_request is None
    assert approved.escalation is None


def test_an_escalation_outranks_an_approval(offline_graph):
    """A run that could not gather its evidence is not waiting for permission."""
    state = run(CLEAN, offline_graph)
    state.escalation = run(CONFLICTED, offline_graph).escalation

    assert interrupt_type(state) == "ESCALATION"


def test_an_escalated_run_never_reaches_drafting(offline_graph):
    state = run(CONFLICTED, offline_graph)

    assert state.draft_sections == {}
    assert state.policy_findings == []


def test_review_routes_back_to_drafting_when_a_note_must_be_fixed(offline_graph):
    state = run(CLEAN, offline_graph)
    state.review_notes = [
        type("N", (), {"must_fix": True, "note": "x", "category": "unsourced_figure"})()
    ]
    state.revision_count = 0

    assert offline_graph.after_review(state) == "drafting"


def test_review_stops_routing_back_at_the_revision_cap(offline_graph):
    state = run(CLEAN, offline_graph)
    state.review_notes = [
        type("N", (), {"must_fix": True, "note": "x", "category": "unsourced_figure"})()
    ]
    state.revision_count = offline_graph.max_revisions

    assert offline_graph.after_review(state) == "approval"


def test_an_approved_memo_never_routes_back_to_drafting(offline_graph):
    """Drafting is forbidden from running once the memo is approved, so routing
    back to it would deadlock the pair of nodes."""
    state = run(CLEAN, offline_graph)
    state.approved_memo = "# final"
    state.review_notes = [
        type("N", (), {"must_fix": True, "note": "x", "category": "unsourced_figure"})()
    ]

    assert offline_graph.after_review(state) == "approval"


# --- Nothing is written before approval -------------------------------------


def test_no_file_is_written_before_approval(offline_graph, tmp_path):
    run(CLEAN, offline_graph)
    output = tmp_path / "output"

    assert not output.exists() or list(output.iterdir()) == []


def test_an_escalated_run_writes_nothing(offline_graph, tmp_path):
    state = run(CONFLICTED, offline_graph)
    output = tmp_path / "output"

    assert state.rendered_path is None
    assert not output.exists() or list(output.iterdir()) == []
    assert "Nothing has been written" in state.escalation.detail


def test_the_render_node_refuses_to_run_without_an_approval(offline_graph):
    state = run(CLEAN, offline_graph)

    with pytest.raises(RuntimeError, match="no write is permitted"):
        offline_graph.finalise(state)


def test_approval_then_render_writes_exactly_one_file(offline_graph):
    state = approve(run(CLEAN, offline_graph), "P. Narayanasamy")
    state = offline_graph.finalise(state)
    written = list(offline_graph.output_dir.iterdir())

    assert len(written) == 1
    assert Path(state.rendered_path).exists()
    assert Path(state.rendered_path).suffix == ".docx"
    assert Path(state.rendered_path).stat().st_size > 0


def test_rejecting_writes_nothing_and_retains_no_memo(offline_graph):
    state = reject(run(CLEAN, offline_graph), "P. Narayanasamy", "figures need checking")

    assert state.approved_memo is None
    assert state.approval_request is None
    assert state.rendered_path is None
    assert not offline_graph.output_dir.exists()


# --- Approval ---------------------------------------------------------------


def test_approving_captures_the_memo_and_the_approver(offline_graph):
    state = approve(run(CLEAN, offline_graph), "P. Narayanasamy")

    assert state.approved_by == "P. Narayanasamy"
    assert state.approved_memo.startswith("# Credit memo")
    assert "P. Narayanasamy" in state.approved_memo


def test_approving_with_edits_keeps_the_edited_text(offline_graph):
    state = approve(run(CLEAN, offline_graph), "P. Narayanasamy", edited_memo="# My version\n")

    assert state.approved_memo == "# My version\n"


def test_approving_twice_does_not_regenerate_the_memo(offline_graph):
    first = approve(run(CLEAN, offline_graph), "P. Narayanasamy")
    second = approve(first, "Someone Else")

    assert second.approved_memo == first.approved_memo
    assert second.approved_by == "P. Narayanasamy"


def test_approving_something_never_submitted_raises(offline_graph):
    with pytest.raises(RuntimeError, match="nothing has been submitted"):
        approve(MemoState(application_number=CLEAN), "P. Narayanasamy")


def test_drafting_does_not_run_again_once_approved(offline_graph):
    state = approve(run(CLEAN, offline_graph), "P. Narayanasamy")
    original = dict(state.draft_sections)

    result = offline_graph.drafting(state)

    assert result["draft_sections"] == original
    assert "already approved" in result["trace"][-1]


def test_the_approval_request_says_nothing_has_been_written(offline_graph):
    request = run(CLEAN, offline_graph).approval_request

    assert "Nothing has been written" in request.summary
    assert request.figure_count > 0


# --- Escalation resolution --------------------------------------------------


def test_an_analyst_supplied_value_enters_the_ledger_with_its_own_provenance(offline_graph):
    state = run(CONFLICTED, offline_graph)
    state = resolve_escalation(
        state, "borrower_1_full_name", "Kathryn Ellingham", "P. Narayanasamy",
        "confirmed against the passport",
    )
    item = state.ledger["borrower_1_full_name"]

    assert item.provenance.source_kind == "analyst"
    assert "P. Narayanasamy" in item.provenance.describe()
    assert state.escalation is None


def test_a_resolved_escalation_lets_the_run_continue_to_approval(offline_graph):
    state = run(CONFLICTED, offline_graph)
    state = resolve_escalation(
        state, "borrower_1_full_name", "Kathryn Ellingham", "P. Narayanasamy")
    state = MemoState.model_validate(offline_graph.build().invoke(state))

    assert interrupt_type(state) == "APPROVAL"


def test_an_analyst_value_survives_the_agent_running_again(offline_graph):
    """Re-running the evidence loop re-detects the same disagreement. It must not
    quietly discard the adjudication that let the run continue."""
    state = run(CONFLICTED, offline_graph)
    state = resolve_escalation(
        state, "borrower_1_full_name", "Kathryn Ellingham", "P. Narayanasamy")
    state = MemoState.model_validate(offline_graph.build().invoke(state))

    assert state.ledger["borrower_1_full_name"].value == "Kathryn Ellingham"
    assert state.ledger["borrower_1_full_name"].provenance.source_kind == "analyst"
    assert "borrower_1_full_name" not in state.unresolved
    assert any("still disagree" in line for line in state.trace)


def test_an_escalation_not_fully_resolved_stays_escalated(offline_graph):
    state = run("APP-2026-0003", offline_graph)
    state = resolve_escalation(
        state, "borrower_2_payg_income", 105_300_00, "P. Narayanasamy",
        "employment letter received outside the file",
    )

    assert state.escalation is not None, "other required fields are still unresolved"


# --- State persistence ------------------------------------------------------


def test_state_survives_a_checkpoint_round_trip(offline_graph):
    state = approve(run(CLEAN, offline_graph), "P. Narayanasamy")
    restored = MemoState.model_validate_json(state.model_dump_json())

    assert restored.approved_memo == state.approved_memo
    assert restored.ledger.keys() == state.ledger.keys()
    assert restored.template_id == state.template_id


def test_a_missing_application_escalates_rather_than_crashing(offline_graph):
    state = run("APP-9999-9999", offline_graph)

    assert interrupt_type(state) == "ESCALATION"
    assert "does not exist" in state.escalation.detail


# --- An unreachable drafting model ------------------------------------------


class BrokenDrafter:
    """Stands in for a model that is unreachable - expired credit, network, 500."""

    name = "broken"

    def _fail(self, brief):
        raise RuntimeError("Error code: 400 - credit balance is too low")

    file_overview = risk_observations = outstanding_items = _fail


def test_an_unreachable_drafting_model_escalates_rather_than_crashing(offline_graph):
    """A model failure is a value returned into state, not an exception escaping
    the node. The analyst gets a readable escalation, not a traceback."""
    from src.agents.drafting import drafting_node

    offline_graph.drafting = lambda state: drafting_node(state, BrokenDrafter())
    state = run(CLEAN, offline_graph)

    assert interrupt_type(state) == "ESCALATION"
    assert "could not be drafted" in state.escalation.summary
    assert "credit balance" in state.escalation.detail


def test_a_drafting_failure_keeps_the_evidence_it_gathered(offline_graph):
    from src.agents.drafting import drafting_node

    offline_graph.drafting = lambda state: drafting_node(state, BrokenDrafter())
    state = run(CLEAN, offline_graph)

    assert len(state.ledger) > 30, "the ledger survives a drafting failure"
    assert state.policy_findings, "analysis ran before drafting"
    assert str(len(state.ledger)) in state.escalation.detail


def test_a_drafting_failure_writes_nothing(offline_graph):
    from src.agents.drafting import drafting_node

    offline_graph.drafting = lambda state: drafting_node(state, BrokenDrafter())
    state = run(CLEAN, offline_graph)

    assert state.rendered_path is None
    assert state.approved_memo is None
    assert "Nothing has been written" in state.escalation.detail
    assert not offline_graph.output_dir.exists()


def test_a_drafting_failure_does_not_reach_the_reviewer(offline_graph):
    from src.agents.drafting import drafting_node

    offline_graph.drafting = lambda state: drafting_node(state, BrokenDrafter())
    state = run(CLEAN, offline_graph)

    assert state.review_notes == [], "there is nothing to review"
    assert state.draft_sections == {}
