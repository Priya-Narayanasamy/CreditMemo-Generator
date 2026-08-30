"""Phase 5 tests: the analysis, drafting and review agents.

The load-bearing test in this file is that no figure reaches the narrative unless
it is in the evidence ledger. The reviewer's figure check is a pure function, and
these tests attack it directly rather than only through a happy path.
"""

from __future__ import annotations

import pytest

from config import DB_PATH, DOCUMENTS_DIR
from src.agents.analysis import AnalysisAgent, analysis_node
from src.agents.drafting import (
    FileOverview,
    OfflineDrafter,
    OutstandingItems,
    RiskObservation,
    RiskObservations,
    build_brief,
    drafting_node,
    figures_in,
)
from src.agents.evidence import EvidenceAgent, evidence_node
from src.agents.review import (
    NoModelReviewer,
    ReviewAgent,
    ReviewCritique,
    assessment_language,
    permitted_figures,
    review_node,
    unmentioned_unresolved,
    unsourced_figures,
)
from src.state import MemoState
from src.tools.extraction import LocalTableExtractor

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)


def apply(state: MemoState, update: dict) -> MemoState:
    for key, value in update.items():
        setattr(state, key, value)
    return state


@pytest.fixture(scope="module")
def prepared() -> dict[str, MemoState]:
    """Evidence and analysis run for each application, ready to draft from."""
    states = {}
    for number in ("APP-2026-0001", "APP-2026-0002", "APP-2026-0003",
                   "APP-2026-0004", "APP-2026-0006"):
        state = MemoState(application_number=number)
        apply(state, evidence_node(state, EvidenceAgent(extractor=LocalTableExtractor())))
        apply(state, analysis_node(state))
        states[number] = state
    return states


@pytest.fixture()
def drafted(prepared) -> MemoState:
    state = prepared["APP-2026-0001"].model_copy(deep=True)
    return apply(state, drafting_node(state, OfflineDrafter(state)))


# --- Analysis ---------------------------------------------------------------


def test_analysis_evaluates_the_ruleset_against_the_ledger(prepared):
    findings = prepared["APP-2026-0001"].policy_findings

    assert findings
    assert {f.status for f in findings} <= {"within_parameter", "outside_parameter", "not_evaluable"}


def test_analysis_computes_nothing_itself(prepared):
    """The figures were computed during the evidence loop. Analysis reads them."""
    state = prepared["APP-2026-0001"]
    facts = AnalysisAgent().application_facts(state)

    assert str(facts["computed_lvr"]) == state.ledger["computed_lvr"].value


def test_an_unresolved_field_makes_its_rule_not_evaluable(prepared):
    """Never silently within parameter."""
    findings = {f.rule_id: f for f in prepared["APP-2026-0003"].policy_findings}

    assert findings["SERVICEABILITY_MIN_COVERAGE"].status == "not_evaluable"
    assert findings["SERVICEABILITY_MIN_COVERAGE"].finding_type == "missing"


def test_a_conflicting_lvr_leaves_the_lvr_rules_not_evaluable(prepared):
    """Seeded defect 6. The LVR is disputed, so no rule about it can be evaluated."""
    findings = {f.rule_id: f for f in prepared["APP-2026-0006"].policy_findings}

    assert findings["LVR_MAX_OO"].status == "not_evaluable"


def test_an_identity_conflict_becomes_a_discrepancy_finding(prepared):
    findings = [
        f for f in prepared["APP-2026-0004"].policy_findings
        if f.rule_id == "IDENTITY_SOURCES_AGREE"
    ]

    assert len(findings) == 1
    assert findings[0].finding_type == "discrepancy"
    assert "Katherine Ellingham" in findings[0].message


def test_a_listed_default_is_a_note_and_does_not_block(prepared):
    finding = next(
        f for f in prepared["APP-2026-0003"].policy_findings
        if f.rule_id == "NO_LISTED_DEFAULTS" and f.status == "outside_parameter"
    )

    assert finding.finding_type == "note"


def test_identity_status_is_per_borrower(prepared):
    agent = AnalysisAgent()

    assert agent.identity_status(prepared["APP-2026-0004"], 1) == "conflict"
    assert agent.identity_status(prepared["APP-2026-0002"], 1) == "match"
    assert agent.identity_status(prepared["APP-2026-0002"], 2) == "match"


def test_sufficiency_notes_say_why_a_rule_could_not_be_evaluated(prepared):
    state = prepared["APP-2026-0003"]
    notes = AnalysisAgent().sufficiency_notes(state, state.policy_findings)

    assert any("SERVICEABILITY_MIN_COVERAGE" in note for note in notes)
    assert any("unresolved" in note for note in notes)


def test_analysis_is_deterministic(prepared):
    state = prepared["APP-2026-0001"]
    runs = {
        tuple((f.rule_id, f.subject, f.status) for f in AnalysisAgent().run(state))
        for _ in range(5)
    }

    assert len(runs) == 1


# --- The brief --------------------------------------------------------------


def test_the_brief_contains_every_ledger_value(prepared):
    state = prepared["APP-2026-0001"]
    brief = build_brief(state)

    for name in state.ledger:
        assert name in brief


def test_the_brief_lists_unresolved_fields_without_supplying_values(prepared):
    brief = build_brief(prepared["APP-2026-0004"])

    assert "UNRESOLVED" in brief
    assert "borrower_1_full_name: conflict" in brief


def test_the_brief_renders_money_as_currency(prepared):
    assert "$585,000.00" in build_brief(prepared["APP-2026-0001"])


# --- Drafting ---------------------------------------------------------------


def test_drafting_produces_exactly_the_declared_sections(drafted):
    assert set(drafted.draft_sections) == {
        "file_overview", "risk_observations", "outstanding_items"
    }


def test_risk_observations_are_exactly_three(drafted):
    lines = [line for line in drafted.draft_sections["risk_observations"].splitlines() if line.strip()]

    assert len(lines) == 3


def test_the_schema_refuses_a_wrong_number_of_observations():
    """The count is enforced by the schema, not by asking the model nicely."""
    observation = RiskObservation(category="security", body="One. Two.")

    with pytest.raises(Exception):
        RiskObservations(observations=[observation, observation])


def test_drafting_does_not_run_once_the_memo_is_approved(prepared):
    """Once approved_memo is set, the drafting node must not run again."""
    state = prepared["APP-2026-0001"].model_copy(deep=True)
    state.approved_memo = "# final text"
    state.draft_sections = {"file_overview": "original"}

    result = drafting_node(state, OfflineDrafter(state))

    assert result["draft_sections"] == {"file_overview": "original"}
    assert "already approved" in result["trace"][-1]


def test_the_draft_names_the_unresolved_fields(prepared):
    state = prepared["APP-2026-0003"].model_copy(deep=True)
    apply(state, drafting_node(state, OfflineDrafter(state)))

    assert "borrower_2_payg_income" in state.draft_sections["outstanding_items"]


def test_the_draft_states_no_income_figure_when_income_is_unresolved(prepared):
    state = prepared["APP-2026-0003"].model_copy(deep=True)
    apply(state, drafting_node(state, OfflineDrafter(state)))

    assert "could not be established" in state.draft_sections["risk_observations"]


def test_a_conflicting_field_is_reported_with_both_values(prepared):
    state = prepared["APP-2026-0004"].model_copy(deep=True)
    apply(state, drafting_node(state, OfflineDrafter(state)))
    items = state.draft_sections["outstanding_items"]

    assert "sources disagree" in items
    assert "Katherine Ellingham" in items and "Kathryn Ellingham" in items


def test_drafting_is_deterministic_at_figure_level(prepared):
    state = prepared["APP-2026-0001"]
    runs = [drafting_node(state.model_copy(deep=True),
                          OfflineDrafter(state))["draft_sections"] for _ in range(5)]
    figure_sets = [
        {figure for body in sections.values() for figure in figures_in(body)}
        for sections in runs
    ]

    assert all(figures == figure_sets[0] for figures in figure_sets)


# --- Figure extraction ------------------------------------------------------


def test_figures_in_finds_currency_ratios_and_counts():
    found = figures_in("The LVR is 0.7500 on $585,000.00 across 3 splits.")

    assert "$585,000.00" in found
    assert "0.7500" in found
    assert "3" in found


def test_figures_in_handles_empty_text():
    assert figures_in("") == set()


# --- The unsourced-figure check ---------------------------------------------


def test_a_clean_draft_has_no_unsourced_figures(drafted):
    assert unsourced_figures(drafted, drafted.draft_sections) == []


def test_an_invented_figure_is_caught(drafted):
    sections = dict(drafted.draft_sections)
    sections["risk_observations"] += " Annual outgoings are approximately $91,433.17."

    found = unsourced_figures(drafted, sections)

    assert ("risk_observations", "$91,433.17") in found


def test_a_figure_that_is_arithmetically_true_but_unsourced_is_still_caught(drafted):
    """The rule is that a figure must be in the ledger, not that it must be
    correct. A model doing its own arithmetic is exactly what this prevents."""
    loan = drafted.ledger["total_loan_amount"].value
    valuation = drafted.ledger["valuation_amount"].value
    gap = f"${(valuation - loan) // 100:,}"

    found = unsourced_figures(drafted, {"file_overview": f"Equity of {gap} remains."})

    assert found and found[0][1] == gap


def test_a_ledger_figure_is_permitted_in_any_of_its_written_forms(drafted):
    allowed = permitted_figures(drafted)

    assert "$585,000.00" in allowed
    assert "585,000.00" in allowed
    assert "0.7500" in allowed


def test_small_word_numbers_are_not_treated_as_figures(drafted):
    assert unsourced_figures(drafted, {"file_overview": "There are three documents."}) == []


# --- Assessment language ----------------------------------------------------


@pytest.mark.parametrize(
    "sentence, phrase",
    [
        ("This application is recommended for approval.", "recommend"),
        ("The borrower can service the loan comfortably.", "can service"),
        ("The LVR breach is material.", "breach"),
        ("Assigned a risk grade of B.", "risk grade"),
        ("The deal fails policy on serviceability.", "failed policy"),
    ],
)
def test_assessment_language_is_caught(sentence, phrase):
    found = assessment_language({"risk_observations": sentence})

    assert found, f"{sentence!r} should have been flagged"


def test_neutral_language_is_not_flagged(drafted):
    assert assessment_language(drafted.draft_sections) == []


# --- Omissions --------------------------------------------------------------


def test_an_unmentioned_unresolved_field_is_reported(prepared):
    state = prepared["APP-2026-0004"]

    assert unmentioned_unresolved(state, {"file_overview": "All is well."}) == [
        "borrower_1_full_name"
    ]


def test_a_mentioned_unresolved_field_is_not_reported(prepared):
    state = prepared["APP-2026-0004"]
    sections = {"outstanding_items": "borrower_1_full_name: sources disagree."}

    assert unmentioned_unresolved(state, sections) == []


# --- The review node --------------------------------------------------------


def test_a_clean_draft_produces_no_must_fix_notes(drafted):
    result = review_node(drafted, ReviewAgent(NoModelReviewer()))

    assert [note for note in result["review_notes"] if note.must_fix] == []
    assert result["revision_count"] == 0


def test_an_unsourced_figure_routes_the_draft_back_to_drafting(drafted):
    drafted.draft_sections["file_overview"] += " Total exposure is $999,999.00."
    agent = ReviewAgent(NoModelReviewer())
    notes = agent.review(drafted)

    assert any(note.category == "unsourced_figure" and note.must_fix for note in notes)
    assert agent.should_revise(drafted, notes)


def test_revisions_are_capped(drafted):
    drafted.draft_sections["file_overview"] += " Total exposure is $999,999.00."
    agent = ReviewAgent(NoModelReviewer(), max_revisions=2)
    notes = agent.review(drafted)
    drafted.revision_count = 2

    assert not agent.should_revise(drafted, notes)


def test_unaddressed_notes_are_carried_to_the_analyst_not_retried(drafted):
    drafted.draft_sections["file_overview"] += " Total exposure is $999,999.00."
    drafted.revision_count = 2
    result = review_node(drafted, ReviewAgent(NoModelReviewer(), max_revisions=2))

    assert "carried to the analyst" in " ".join(result["trace"])
    assert result["revision_count"] == 2


def test_the_model_critique_is_additive_to_the_deterministic_checks(drafted):
    class Critic:
        name = "stub"

        def critique(self, sections, brief):
            return ReviewCritique(
                omissions=["the valuation date is not mentioned"],
                inconsistencies=["the document count disagrees with the brief"],
                wording=["'comfortably' editorialises"],
            )

    notes = ReviewAgent(Critic()).review(drafted)
    categories = {note.category for note in notes}

    assert {"omission", "inconsistency", "wording"} <= categories
    assert any(note.category == "inconsistency" and note.must_fix for note in notes)


def test_a_reviewer_that_fails_does_not_lose_the_deterministic_findings(drafted):
    class Broken:
        name = "broken"

        def critique(self, sections, brief):
            raise RuntimeError("model unavailable")

    drafted.draft_sections["file_overview"] += " Total exposure is $999,999.00."

    with pytest.raises(RuntimeError):
        ReviewAgent(Broken()).review(drafted)

    # The deterministic checks stand on their own, without any reviewer at all.
    notes = ReviewAgent(NoModelReviewer()).review(drafted)
    assert any(note.category == "unsourced_figure" for note in notes)


def test_review_is_deterministic(drafted):
    agent = ReviewAgent(NoModelReviewer())
    runs = {
        tuple((n.category, n.section, n.note) for n in agent.review(drafted))
        for _ in range(5)
    }

    assert len(runs) == 1
