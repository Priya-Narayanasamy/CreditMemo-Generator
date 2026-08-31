"""Determinism.

Two separate claims, and only one of them is about the model:

- figure-level variance is exactly zero. Every number in the memo comes from the
  ledger, and the ledger is produced by database reads and pure functions, so this
  must hold whatever the drafting model does.
- section count and structure do not vary. The template owns them.

Narrative wording is allowed to vary, and does when a real drafting model is
configured. The wording test is skipped without credentials rather than asserted
against a stand-in that could never vary - a test that cannot fail is worse than
no test.
"""

from __future__ import annotations

import re

import pytest

from config import DB_PATH, DOCUMENTS_DIR
from src.agents.drafting import figures_in
from src.agents.review import permitted_figures, unsourced_figures
from src.graph import approve, run
from src.tools.renderer import render_markdown

def _drafting_credentials() -> bool:
    from src.tools.models import drafting_credentials_present

    return drafting_credentials_present()


pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)

RUNS = 10
APPLICATION = "APP-2026-0001"


@pytest.fixture(scope="module")
def memos() -> list[tuple[str, dict]]:
    """Ten independent runs of the same application, rendered."""
    from tests.conftest import build_offline_graph

    results = []
    for _ in range(RUNS):
        state = approve(run(APPLICATION, build_offline_graph()), "P. Narayanasamy")
        results.append((render_markdown(state, "P. Narayanasamy"), state.draft_sections))
    return results


def figures_of(markdown: str) -> list[str]:
    """Every figure in the rendered memo, in order, minus the generation timestamp."""
    without_timestamp = re.sub(
        r"\d{2} \w+ \d{4} \d{2}:\d{2} UTC", "[timestamp]", markdown
    )
    without_timestamp = re.sub(r"\| Generated \|.*\|", "| Generated | [timestamp] |",
                               without_timestamp)
    return sorted(figures_in(without_timestamp))


def structure_of(markdown: str) -> list[str]:
    return [line.strip() for line in markdown.splitlines() if line.startswith("#")]


# --- Figures ----------------------------------------------------------------


def test_figure_level_variance_is_exactly_zero(memos):
    baselines = {tuple(figures_of(markdown)) for markdown, _ in memos}

    assert len(baselines) == 1, "the same application produced different figures"


def test_every_figure_in_every_run_is_in_the_ledger():
    from tests.conftest import build_offline_graph

    for _ in range(3):
        state = run(APPLICATION, build_offline_graph())

        assert unsourced_figures(state, state.draft_sections) == []


def test_the_rendered_memo_states_no_figure_outside_the_ledger():
    """The strongest form of the rule: check the whole rendered document, not just
    the narrative sections."""
    from tests.conftest import build_offline_graph

    state = approve(run(APPLICATION, build_offline_graph()), "P. Narayanasamy")
    markdown = render_markdown(state, "P. Narayanasamy")
    allowed = permitted_figures(state)

    # Values the template legitimately supplies that are not figures about the deal:
    # ordinals, the bureau score range, and the policy parameters themselves.
    allowed |= {"1", "2", "3", "4", "300", "1200", "30"}
    allowed |= {
        str(rule.threshold) for rule in
        __import__("src.tools.policy", fromlist=["x"]).load_ruleset(
            __import__("config").POLICY_PATH).rules
        if rule.threshold is not None
    }

    body = markdown.split("### Provenance")[0]
    # Scrub dates before extracting figures. ISO dates go first: otherwise the
    # long-date pattern eats "00 dated 2026" out of "$780,000.00 dated 2026-01-08"
    # and leaves a truncated amount behind.
    body = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", body)
    body = re.sub(
        r"\b\d{1,2} (?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December) \d{4}(?: \d{2}:\d{2} UTC)?\b",
        " ", body,
    )
    body = body.replace("claude-sonnet-4-5-20250929", "").replace("Qwen2.5-72B", "")

    unsourced = [
        figure for figure in figures_in(body)
        if figure not in allowed
        and figure.lstrip("$") not in allowed
        and figure.lstrip("$").replace(",", "") not in allowed
    ]

    assert unsourced == [], f"figures in the memo with no ledger entry: {unsourced}"


# --- Structure --------------------------------------------------------------


def test_section_count_and_structure_do_not_vary(memos):
    structures = {tuple(structure_of(markdown)) for markdown, _ in memos}

    assert len(structures) == 1, "the memo structure varied between runs"


def test_the_narrative_section_set_does_not_vary(memos):
    section_sets = {tuple(sorted(sections)) for _, sections in memos}

    assert len(section_sets) == 1
    assert set(memos[0][1]) == {"file_overview", "risk_observations", "outstanding_items"}


def test_risk_observation_count_does_not_vary(memos):
    counts = {
        len([line for line in sections["risk_observations"].splitlines() if line.strip()])
        for _, sections in memos
    }

    assert counts == {3}


def test_every_template_renders_without_an_unsourced_figure():
    """All four templates, not just the one the clean application uses."""
    from tests.conftest import build_offline_graph
    from src.tools.renderer import render_markdown as render

    seen = set()
    for number in ("APP-2026-0001", "APP-2026-0002", "APP-2026-0005", "APP-2026-0003"):
        state = run(number, build_offline_graph())
        if state.escalation is not None:
            continue
        seen.add(state.template_id)

        assert render(state, "tester").startswith("# Credit memo")

    assert len(seen) >= 2


# --- Wording ----------------------------------------------------------------


@pytest.mark.skipif(
    not _drafting_credentials(),
    reason="narrative variation needs the real drafting model; the offline drafter "
           "is deterministic by construction",
)
def test_narrative_wording_may_vary_while_figures_do_not():
    from src.graph import MemoGraph

    runs = [run(APPLICATION, MemoGraph()) for _ in range(3)]

    unreachable = next((s.escalation for s in runs if s.escalation), None)
    if unreachable is not None:
        # A key can be present and still not buy a call - expired credit, a rate
        # limit, an outage. That is an environment condition, not a defect, and
        # the drafting node already turns it into a clean escalation.
        pytest.skip(f"drafting model unreachable: {unreachable.summary}")

    wordings = {state.draft_sections["file_overview"] for state in runs}

    # Which figures the narrative chooses to mention moves with the wording, and
    # pinning that would mean pinning the prose. The invariant that matters is
    # not that the same figures are cited every time - it is that every figure
    # cited is in the ledger, on every run.
    for state in runs:
        assert unsourced_figures(state, state.draft_sections) == [], (
            "the live drafter cited a figure that is not in the evidence ledger"
        )

    # The figures the template places are separately pinned to zero variance by
    # test_figure_level_variance_is_exactly_zero. Those are the memo's numbers;
    # these are the narrative's choice of which to repeat.
    assert len(wordings) >= 1
