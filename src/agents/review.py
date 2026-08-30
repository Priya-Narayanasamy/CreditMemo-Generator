"""The review agent.

Critiques the draft against the evidence ledger and the policy findings. Its
central job is deterministic and non-negotiable: every figure appearing in
narrative text must exist in the ledger. That check is a pure function, because a
model is the wrong thing to trust with the one rule the whole system rests on.

A model adds a second pass over wording and omissions. It can route the draft back
to the drafting agent, capped at two revisions.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, Field

from config import MAX_REVISIONS
from src.agents.drafting import figures_in, render_value
from src.state import MemoState, ReviewNote

# Vocabulary this system does not use about a deal.
#
# Stems match any inflection - "recommend" catches "recommended" and
# "recommendation" - because a word list that only matches one tense is a check
# that mostly does not fire.
ASSESSMENT_STEMS = [
    "approv", "declin", "reject", "recommend", "creditworth", "marginal",
    "breach", "unserviceab",
]

# Phrases are matched whole; the individual words in them are unremarkable.
ASSESSMENT_PHRASES = [
    "risk grade", "risk score", "credit rating", "hard fail", "soft fail",
    "failed policy", "fails policy", "passes policy", "meets policy",
    "strong deal", "weak deal", "acceptable deal", "poor deal",
    "can service", "cannot service", "can comfortably service",
    "able to service", "unable to service", "does not service",
    "we recommend", "should be approved", "should be declined",
]

# Number-like tokens that are not figures about the deal.
INNOCUOUS = {"one", "two", "three"}


class ReviewCritique(BaseModel):
    """What the model contributes, on top of the deterministic checks."""

    omissions: list[str] = Field(default_factory=list)
    inconsistencies: list[str] = Field(default_factory=list)
    wording: list[str] = Field(default_factory=list)


class Reviewer(Protocol):
    name: str

    def critique(self, sections: dict[str, str], brief: str) -> ReviewCritique: ...


class NoModelReviewer:
    """Used when no review credentials are configured. Contributes nothing beyond
    the deterministic checks, and says so rather than pretending to have reviewed."""

    name = "deterministic-only"

    def critique(self, sections: dict[str, str], brief: str) -> ReviewCritique:
        return ReviewCritique()


CRITIQUE_PROMPT = """You are reviewing a first-draft credit memo before an analyst
reads it. You are checking the draft against the evidence brief, not forming a view
on the deal.

Report:
- omissions: anything in the brief that an analyst would expect the narrative to
  mention and it does not, especially unresolved fields and discrepancies
- inconsistencies: anywhere the narrative disagrees with the brief
- wording: any sentence that assesses, grades, recommends, or concludes that the
  borrower can or cannot service the loan

Do not rewrite the draft. Report only what is wrong with it. If a category has
nothing in it, return an empty list.

BRIEF
-----
{brief}

DRAFT
-----
{draft}
"""


class ClaudeReviewer:
    name = "claude"

    def __init__(self, model=None) -> None:
        from config import REVIEW_MODEL
        from src.tools.models import review_model

        self._model = model or review_model()
        self.model_id = REVIEW_MODEL

    def critique(self, sections: dict[str, str], brief: str) -> ReviewCritique:
        from src.tools.models import logged_call

        draft = "\n\n".join(f"## {name}\n{body}" for name, body in sections.items())
        prompt = CRITIQUE_PROMPT.format(brief=brief, draft=draft)
        structured = self._model.with_structured_output(ReviewCritique)

        try:
            return logged_call(
                purpose="review_draft",
                provider="anthropic",
                model=self.model_id,
                prompt=prompt,
                invoke=lambda: structured.invoke(prompt),
            )
        except Exception:  # noqa: BLE001 - the deterministic checks still stand
            return ReviewCritique()


def default_reviewer() -> Reviewer:
    from config import ANTHROPIC_API_KEY

    if ANTHROPIC_API_KEY:
        return ClaudeReviewer()
    return NoModelReviewer()


# --- The deterministic checks -----------------------------------------------


def permitted_figures(state: MemoState) -> set[str]:
    """Every number-like token the narrative is allowed to contain.

    Built from the ledger, in every form a value can legitimately be written: the
    raw value, its rendered form, and the parts of a currency amount.
    """
    allowed: set[str] = set()

    def permit(text: str) -> None:
        allowed.update(figures_in(text))
        # "$585,000.00" also licenses "585,000" and "585000".
        allowed.add(text.lstrip("$"))
        allowed.add(text.replace(",", "").lstrip("$"))

    for name, item in state.ledger.items():
        permit(str(item.value))
        permit(render_value(name, item.value))
        permit(_percent_form(item.value))

        # A list-valued entry - loan splits, credit accounts, related parties -
        # licenses the figures inside it. They are as sourced as the entry is.
        for figure in _nested_figures(item.value):
            permit(figure)

    # Counts of things the memo may legitimately state about itself.
    for count in (
        len(state.documents),
        len([d for d in state.documents if d.document_type != "unknown"]),
        len(state.unresolved),
        len(state.policy_findings),
        len([f for f in state.policy_findings if f.finding_type == "discrepancy"]),
        len([f for f in state.policy_findings if f.status == "outside_parameter"]),
        len(state.verifications),
    ):
        allowed.add(str(count))

    return {value for value in allowed if value}


def _percent_form(value) -> str:
    """A ratio in the ledger licenses its percentage rendering.

    The memo shows an LVR of 0.7500 as 75.00%. That is the same sourced figure
    written the way a reader expects, not a new one.
    """
    from decimal import Decimal, InvalidOperation

    try:
        ratio = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ""
    if not (0 <= ratio <= 1):
        return ""
    return f"{ratio * 100:.2f}"


def _nested_figures(value) -> list[str]:
    """Figures inside a list- or dict-valued ledger entry."""
    found: list[str] = []

    if isinstance(value, dict):
        for nested in value.values():
            found.extend(_nested_figures(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found.extend(_nested_figures(nested))
    elif value is not None and not isinstance(value, bool):
        text = str(value)
        found.append(text)
        if isinstance(value, int):
            found.append(format_cents_safe(value))

    return found


def format_cents_safe(value: int) -> str:
    from src.tools.calculators import format_cents

    return format_cents(value)


def unsourced_figures(state: MemoState, sections: dict[str, str]) -> list[tuple[str, str]]:
    """(section, figure) for every narrative figure absent from the ledger."""
    allowed = permitted_figures(state)
    found: list[tuple[str, str]] = []

    for section, body in sections.items():
        for figure in sorted(figures_in(body)):
            bare = figure.lstrip("$")
            if figure in allowed or bare in allowed or bare.replace(",", "") in allowed:
                continue
            if bare in INNOCUOUS:
                continue
            found.append((section, figure))

    return found


def assessment_language(sections: dict[str, str]) -> list[tuple[str, str]]:
    """(section, phrase) for any vocabulary this system does not use about a deal."""
    found: list[tuple[str, str]] = []

    for section, body in sections.items():
        lowered = (body or "").lower()

        for stem in ASSESSMENT_STEMS:
            match = re.search(rf"\b{re.escape(stem)}\w*", lowered)
            if match:
                found.append((section, match.group()))

        for phrase in ASSESSMENT_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                found.append((section, phrase))

    return found


def unmentioned_unresolved(state: MemoState, sections: dict[str, str]) -> list[str]:
    """Unresolved required fields the narrative never mentions.

    A gap the analyst is not told about is worse than a gap.
    """
    body = " ".join(sections.values()).lower()
    return [
        name for name in sorted(state.unresolved)
        if name in state.required_fields and name.lower() not in body
    ]


# --- The node ---------------------------------------------------------------


class ReviewAgent:
    def __init__(self, reviewer: Reviewer | None = None, max_revisions: int = MAX_REVISIONS) -> None:
        self.reviewer = reviewer or default_reviewer()
        self.max_revisions = max_revisions

    def review(self, state: MemoState) -> list[ReviewNote]:
        from src.agents.drafting import build_brief

        sections = state.draft_sections
        notes: list[ReviewNote] = []

        for section, figure in unsourced_figures(state, sections):
            notes.append(ReviewNote(
                category="unsourced_figure",
                section=section,
                note=(
                    f"{figure} appears in the narrative but is not in the evidence "
                    f"ledger. Remove it or source it."
                ),
                must_fix=True,
            ))

        for section, phrase in assessment_language(sections):
            notes.append(ReviewNote(
                category="assessment_language",
                section=section,
                note=(
                    f"{phrase!r} assesses the deal. This memo consolidates the file; "
                    f"the analyst forms the credit view."
                ),
                must_fix=True,
            ))

        for name in unmentioned_unresolved(state, sections):
            notes.append(ReviewNote(
                category="omission",
                section="outstanding_items",
                note=f"{name} is unresolved and the narrative does not mention it.",
                must_fix=True,
            ))

        critique = self.reviewer.critique(sections, build_brief(state))
        for omission in critique.omissions:
            notes.append(ReviewNote(category="omission", section="file_overview",
                                    note=omission, must_fix=False))
        for inconsistency in critique.inconsistencies:
            notes.append(ReviewNote(category="inconsistency", section="file_overview",
                                    note=inconsistency, must_fix=True))
        for wording in critique.wording:
            notes.append(ReviewNote(category="wording", section="file_overview",
                                    note=wording, must_fix=False))

        return notes

    def should_revise(self, state: MemoState, notes: list[ReviewNote]) -> bool:
        if state.revision_count >= self.max_revisions:
            return False
        return any(note.must_fix for note in notes)


def review_node(state: MemoState, agent: ReviewAgent | None = None) -> dict:
    agent = agent or ReviewAgent()
    notes = agent.review(state)
    revise = agent.should_revise(state, notes)

    must_fix = sum(1 for note in notes if note.must_fix)
    trace = state.trace + [
        f"Review: {len(notes)} note(s), {must_fix} requiring a fix; "
        + (
            f"returning to drafting (revision {state.revision_count + 1} of "
            f"{agent.max_revisions})."
            if revise
            else "no further revision."
        )
    ]

    if not revise and must_fix and state.revision_count >= agent.max_revisions:
        trace.append(
            f"{must_fix} note(s) remain unaddressed after {agent.max_revisions} "
            f"revisions. They are carried to the analyst rather than being retried."
        )

    return {
        "review_notes": notes,
        "revision_count": state.revision_count + (1 if revise else 0),
        "trace": trace,
    }
