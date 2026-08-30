"""The drafting agent.

Generates the narrative sections and nothing else. Structure, headings, tables,
labels and every conditional sentence come from the Jinja2 template. A figure that
a template can place is never asked of a model.

Sections are generated one at a time against a tight structured schema rather than
in one free-form pass, so the shape of the output is fixed even though the wording
is not.

Departing from the build spec, which lists a `recommendation` section: CLAUDE.md
forbids this system from producing an approval, a decline or a recommendation. The
third section is `outstanding_items` - what the analyst still needs to resolve -
and the drafter is told in the prompt that it is not forming a credit view.
"""

from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from src.state import MemoState, PolicyFinding
from src.tools.calculators import format_cents

SECTION_NAMES = ["file_overview", "risk_observations", "outstanding_items"]

RiskCategory = Literal[
    "security", "income", "credit_history", "documentation", "structure", "policy"
]


class RiskObservation(BaseModel):
    category: RiskCategory
    body: str = Field(description="Exactly two sentences.")


class RiskObservations(BaseModel):
    observations: list[RiskObservation] = Field(min_length=3, max_length=3)


class FileOverview(BaseModel):
    body: str = Field(description="Three to five sentences describing what the file contains.")


class OutstandingItems(BaseModel):
    items: list[str] = Field(
        default_factory=list,
        description="One line per unresolved matter. Empty if there are none.",
    )


class NarrativeDrafter(Protocol):
    name: str

    def file_overview(self, brief: str) -> FileOverview: ...

    def risk_observations(self, brief: str) -> RiskObservations: ...

    def outstanding_items(self, brief: str) -> OutstandingItems: ...


# --- The brief the drafter is given -----------------------------------------


VOCABULARY_RULE = """
You are drafting part of a first-draft credit memo for a credit analyst. You are
consolidating a file, not assessing it.

You must not:
- approve, decline, or recommend anything
- assign a risk grade, risk score or credit rating
- call anything a pass, a fail, a breach or a hard fail
- describe the deal as strong, weak, acceptable or marginal
- conclude that the borrower can or cannot service the loan

You must not state any figure that is not given to you below. Do not compute,
round, convert, total or estimate anything. If a figure you would like to cite is
not in the brief, write around it or say it is unresolved.

The analyst forms the credit view. You describe what the file contains.
"""


def build_brief(state: MemoState) -> str:
    """Everything the drafter is allowed to know, in plain text.

    A figure absent from this brief is a figure the drafter cannot cite, which is
    the first half of keeping the narrative inside the ledger. The review agent is
    the second half.
    """
    lines = [f"APPLICATION: {state.application_number}", ""]

    lines.append("SOURCED FIGURES AND FACTS (the only values you may state):")
    for name, item in sorted(state.ledger.items()):
        lines.append(f"  {name} = {render_value(name, item.value)}")

    if state.unresolved:
        lines.append("")
        lines.append("UNRESOLVED (state these as unresolved; never supply a value):")
        for name, entry in sorted(state.unresolved.items()):
            lines.append(f"  {name}: {entry.reason}")

    if state.policy_findings:
        lines.append("")
        lines.append("POLICY OBSERVATIONS:")
        for finding in state.policy_findings:
            if finding.status == "within_parameter":
                continue
            subject = f" [{finding.subject}]" if finding.subject else ""
            lines.append(f"  {finding.rule_id}{subject}: {finding.status} - {finding.message}")

    return "\n".join(lines)


def render_value(name: str, value) -> str:
    """How a ledger value appears in the brief - the same way it appears in the memo."""
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, int) and _is_money(name):
        return format_cents(value)
    if isinstance(value, list):
        return f"[{len(value)} item(s)]"
    return str(value)


MONEY_SUFFIXES = (
    "_amount", "_income", "_repayments", "_surplus", "amount", "income",
)


def _is_money(name: str) -> bool:
    return name.endswith(MONEY_SUFFIXES)


# --- Offline drafter --------------------------------------------------------


class OfflineDrafter:
    """Deterministic narrative assembled from the ledger.

    Used when no drafting credentials are configured, and by the tests so the
    figure-level determinism check does not depend on a network call. It writes
    plainly and cites only ledger values.
    """

    name = "offline"

    def __init__(self, state: MemoState) -> None:
        self.state = state

    def _value(self, name: str) -> str | None:
        item = self.state.ledger.get(name)
        return render_value(name, item.value) if item else None

    def file_overview(self, brief: str) -> FileOverview:
        state = self.state
        purpose = (self._value("loan_purpose") or "").replace("_", " ")
        structure = self._value("applicant_structure") or ""
        borrowers = [
            state.ledger[name].value
            for name in sorted(state.ledger)
            if name.endswith("_record_name")
        ]

        sentences = [
            f"This file consolidates the application record and supporting documents "
            f"for {state.application_number}, "
            f"{'an' if purpose[:1] in 'aeiou' else 'a'} {purpose} facility with a "
            f"{structure} applicant structure.",
            f"The borrower record names {' and '.join(borrowers)}."
            if borrowers else "The borrower record names no applicant.",
            f"{len(state.documents)} supporting documents were located, of which "
            f"{sum(1 for d in state.documents if d.document_type != 'unknown')} were "
            f"classified and read.",
        ]

        if self._value("total_loan_amount") and self._value("security_address"):
            sentences.append(
                f"The facility of {self._value('total_loan_amount')} is secured against "
                f"{self._value('security_address')}."
            )

        sentences.append(
            f"{len(state.unresolved)} field(s) remain unresolved and are listed below."
            if state.unresolved
            else "Every field required by the template resolved to a sourced value."
        )
        return FileOverview(body=" ".join(sentences))

    def risk_observations(self, brief: str) -> RiskObservations:
        state = self.state
        observations: list[RiskObservation] = []

        lvr = self._value("computed_lvr")
        if lvr:
            observations.append(RiskObservation(
                category="security",
                body=(
                    f"The calculated loan to valuation ratio is {lvr}, against a "
                    f"valuation of {self._value('valuation_amount')} dated "
                    f"{self._value('valuation_date')}. The valuation was "
                    f"{self._value('valuation_age_days')} days old at lodgement."
                ),
            ))
        else:
            observations.append(RiskObservation(
                category="security",
                body=(
                    "The loan to valuation ratio is unresolved and is not stated here. "
                    "The security position cannot be described from the file as it stands."
                ),
            ))

        income = self._value("computed_total_annual_income")
        if income:
            observations.append(RiskObservation(
                category="income",
                body=(
                    f"Annualised PAYG income across the applicants is {income}, drawn "
                    f"from the payslips on file. Assessed annual outgoings are "
                    f"{self._value('computed_assessed_repayments')}."
                ),
            ))
        else:
            observations.append(RiskObservation(
                category="income",
                body=(
                    "Annualised income could not be established from the payslips on "
                    "file. No income figure is stated in this memo."
                ),
            ))

        discrepancies = [f for f in state.policy_findings if f.finding_type == "discrepancy"]
        if discrepancies:
            observations.append(RiskObservation(
                category="documentation",
                body=(
                    f"{len(discrepancies)} discrepancy or discrepancies were recorded "
                    f"between the application record and the supporting documents. "
                    f"Each is listed in the policy observations table for the analyst "
                    f"to reconcile."
                ),
            ))
        else:
            observations.append(RiskObservation(
                category="credit_history",
                body=(
                    "Bureau reports were located and read for each applicant on the "
                    "file. The scores, enquiry counts and listed defaults appear in "
                    "the credit history table."
                ),
            ))

        return RiskObservations(observations=observations[:3])

    def outstanding_items(self, brief: str) -> OutstandingItems:
        items = []
        for name, entry in sorted(self.state.unresolved.items()):
            if entry.is_conflict:
                values = " against ".join(repr(v.value) for v in entry.conflicting_values)
                items.append(f"{name}: sources disagree - {values}.")
            else:
                items.append(f"{name}: not resolved from the file ({entry.reason}).")
        return OutstandingItems(items=items)


# --- Claude drafter ---------------------------------------------------------


SECTION_PROMPTS = {
    "file_overview": (
        "Write the file overview: three to five sentences describing what this "
        "application file contains and what was read to compile it."
    ),
    "risk_observations": (
        "Write exactly three risk observations. Each has a category and a body of "
        "exactly two sentences. Choose the three matters most worth an analyst's "
        "attention. Describe them; do not weigh them."
    ),
    "outstanding_items": (
        "List what the analyst still needs to resolve before this file is complete, "
        "one line each. Only list matters shown as unresolved or outside a policy "
        "parameter in the brief. If there are none, return an empty list."
    ),
}


class ClaudeDrafter:
    """The production drafter. Structured output, temperature zero, every call logged."""

    name = "claude"

    def __init__(self, model=None) -> None:
        from config import DRAFTING_MODEL
        from src.tools.models import drafting_model

        self._model = model or drafting_model()
        self.model_id = DRAFTING_MODEL

    def _section(self, section: str, brief: str, schema):
        from src.tools.models import logged_call

        prompt = f"{VOCABULARY_RULE}\n{SECTION_PROMPTS[section]}\n\nBRIEF\n-----\n{brief}\n"
        structured = self._model.with_structured_output(schema)

        return logged_call(
            purpose=f"draft_{section}",
            provider="anthropic",
            model=self.model_id,
            prompt=prompt,
            invoke=lambda: structured.invoke(prompt),
        )

    def file_overview(self, brief: str) -> FileOverview:
        return self._section("file_overview", brief, FileOverview)

    def risk_observations(self, brief: str) -> RiskObservations:
        return self._section("risk_observations", brief, RiskObservations)

    def outstanding_items(self, brief: str) -> OutstandingItems:
        return self._section("outstanding_items", brief, OutstandingItems)


def default_drafter(state: MemoState) -> NarrativeDrafter:
    from config import ANTHROPIC_API_KEY

    if ANTHROPIC_API_KEY:
        return ClaudeDrafter()
    return OfflineDrafter(state)


# --- The node ---------------------------------------------------------------


def render_sections(drafter: NarrativeDrafter, brief: str) -> dict[str, str]:
    """Generate each section separately, then flatten to text for the template."""
    overview = drafter.file_overview(brief)
    risks = drafter.risk_observations(brief)
    outstanding = drafter.outstanding_items(brief)

    return {
        "file_overview": overview.body.strip(),
        "risk_observations": "\n".join(
            f"{observation.category.replace('_', ' ').title()}: {observation.body.strip()}"
            for observation in risks.observations
        ),
        "outstanding_items": "\n".join(f"- {item.strip()}" for item in outstanding.items),
    }


def drafting_node(
    state: MemoState,
    drafter: NarrativeDrafter | None = None,
    revision_notes: list[str] | None = None,
) -> dict:
    """Draft the narrative sections.

    Once `approved_memo` is set the memo is final and this node must not run
    again - resuming from a checkpoint restores the approved text rather than
    regenerating it.
    """
    if state.approved_memo is not None:
        return {
            "draft_sections": state.draft_sections,
            "trace": state.trace + ["Drafting skipped: the memo is already approved."],
        }

    drafter = drafter or default_drafter(state)
    brief = build_brief(state)

    if revision_notes:
        brief += "\n\nREVISION NOTES FROM THE REVIEWER (address each one):\n" + "\n".join(
            f"  - {note}" for note in revision_notes
        )

    sections = render_sections(drafter, brief)

    return {
        "draft_sections": sections,
        "trace": state.trace + [
            f"Drafted {len(sections)} narrative sections with the {drafter.name} drafter"
            + (f" (revision {state.revision_count + 1})" if revision_notes else "")
            + "."
        ],
    }


# --- Figure extraction, shared with the reviewer ----------------------------

FIGURE_PATTERN = re.compile(r"\$[\d,]+(?:\.\d{2})?|\b\d+\.\d+\b|\b\d[\d,]*\b")


def figures_in(text: str) -> set[str]:
    """Every number-like token in a piece of narrative.

    Deliberately over-inclusive. The reviewer checks each one against the ledger,
    and a false positive costs a lookup while a false negative lets an unsourced
    figure into the memo.
    """
    return {match.group().strip() for match in FIGURE_PATTERN.finditer(text or "")}
