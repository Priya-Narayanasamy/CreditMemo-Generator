"""The evidence ledger and the graph state.

This is the spine of the system. Everything else writes into it.

Two rules are enforced here rather than left to callers:

- Retry budget is tracked per field. Exhausting attempts on one field cannot
  consume another field's budget.
- A conflict is not a gap. Two sources disagreeing goes straight to escalation
  and bypasses the retry budget entirely, because retrying cannot resolve it.

No type in this module carries an assessment. Findings are `discrepancy`,
`missing` or `note`; policy results are `within_parameter`, `outside_parameter`
or `not_evaluable`. Nothing approves, declines, grades or rates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceKind = Literal["database", "document", "computed", "analyst"]

AttemptOutcome = Literal[
    "found",            # the source contained the field
    "not_present",      # the source parsed cleanly and did not contain the field
    "parse_failed",     # the source could not be read
    "extraction_failed",  # the source was read but extraction errored
    "low_confidence",   # a value came back below the confidence floor
]

UnresolvedReason = Literal["not_found", "parse_failed", "conflict", "low_confidence"]

FindingType = Literal["discrepancy", "missing", "note"]

InterruptType = Literal["ESCALATION", "APPROVAL"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Provenance(BaseModel):
    """Where a value came from. Every ledger entry has one; there is no exception."""

    source_kind: SourceKind
    # database: {"table": ..., "column": ..., "row_key": ...}
    # document: {"filename": ..., "page": ..., "document_type": ...}
    # computed: {"inputs": [ledger field names], "function": ...}
    # analyst:  {"supplied_by": ..., "reason": ...}
    detail: dict[str, Any]
    confidence: float | None = None
    retrieved_at: datetime = Field(default_factory=_now)

    def describe(self) -> str:
        """A one-line human-readable source, for escalations and the memo footer."""
        if self.source_kind == "database":
            return (
                f"database {self.detail.get('table')}.{self.detail.get('column')} "
                f"(row {self.detail.get('row_key')})"
            )
        if self.source_kind == "document":
            page = self.detail.get("page")
            page_part = f" page {page}" if page is not None else ""
            return (
                f"{self.detail.get('document_type')} document "
                f"{self.detail.get('filename')}{page_part}"
            )
        if self.source_kind == "computed":
            inputs = ", ".join(self.detail.get("inputs", []))
            return f"computed by {self.detail.get('function')} from {inputs}"
        return f"supplied by analyst {self.detail.get('supplied_by')}"


class EvidenceItem(BaseModel):
    field_name: str
    value: Any
    provenance: Provenance


class Attempt(BaseModel):
    """One attempt to resolve one field from one source.

    The outcome matters, not just the count. A source that parsed cleanly and did
    not contain the field must never be retried; a source that failed to parse may
    be retried with different settings.
    """

    source_kind: SourceKind
    source_ref: str            # filename, or "table.column"
    outcome: AttemptOutcome
    detail: str = ""
    attempted_at: datetime = Field(default_factory=_now)

    @property
    def retryable(self) -> bool:
        return self.outcome in {"parse_failed", "extraction_failed"}


class UnresolvedField(BaseModel):
    field_name: str
    reason: UnresolvedReason
    attempts: list[Attempt] = Field(default_factory=list)
    conflicting_values: list[EvidenceItem] = Field(default_factory=list)

    @property
    def is_conflict(self) -> bool:
        return self.reason == "conflict"

    def tried_sources(self) -> set[str]:
        return {a.source_ref for a in self.attempts}

    def exhausted_sources(self) -> set[str]:
        """Sources there is no point returning to."""
        return {a.source_ref for a in self.attempts if not a.retryable}

    def describe(self) -> str:
        """What was tried and what each source returned.

        An escalation goes to a human analyst. "Extraction failed" is not enough.
        """
        if not self.attempts and not self.conflicting_values:
            return f"{self.field_name}: no source was attempted."

        lines = [f"{self.field_name} ({self.reason})."]

        if self.conflicting_values:
            lines.append("  Sources disagree:")
            for item in self.conflicting_values:
                lines.append(f"    - {item.value!r} from {item.provenance.describe()}")

        if self.attempts:
            lines.append("  Sources attempted:")
            for attempt in self.attempts:
                detail = f" - {attempt.detail}" if attempt.detail else ""
                lines.append(f"    - {attempt.source_ref}: {attempt.outcome}{detail}")

        return "\n".join(lines)


class VerificationResult(BaseModel):
    """A field that exists in both the record and a document resolves to a
    comparison, not to a picked value."""

    field_name: str
    status: Literal["match", "conflict", "not_present"]
    record_value: EvidenceItem | None = None
    document_value: EvidenceItem | None = None

    def describe(self) -> str:
        record = self.record_value.value if self.record_value else "not found"
        document = self.document_value.value if self.document_value else "not found"
        return f"{self.field_name}: record {record!r} against document {document!r}"


class PolicyFinding(BaseModel):
    """The result of evaluating one policy rule against the evidence.

    Deliberately carries no verdict on the deal. `outside_parameter` states that a
    figure sits outside a policy parameter; it does not say the deal fails, and the
    analyst decides what that means.
    """

    rule_id: str
    description: str
    field_name: str
    status: Literal["within_parameter", "outside_parameter", "not_evaluable"]
    finding_type: FindingType
    observed_value: str | None = None
    parameter: str | None = None
    subject: str | None = None      # e.g. which borrower the rule was applied to
    message: str = ""


class ReviewNote(BaseModel):
    category: Literal[
        "unsourced_figure",
        "assessment_language",
        "omission",
        "inconsistency",
        "wording",
    ]
    section: str
    note: str
    must_fix: bool = False


class DocumentRecord(BaseModel):
    """A document belonging to the application, and what became of it."""

    filename: str
    path: str
    document_type: Literal["equifax", "kyc", "payslip", "unknown"] = "unknown"
    classification_confidence: float | None = None
    page_count: int | None = None
    parse_ok: bool | None = None
    parse_error: str | None = None
    subject_name: str | None = None


class Escalation(BaseModel):
    """The agent cannot proceed. Distinct from an approval request."""

    interrupt_type: Literal["ESCALATION"] = "ESCALATION"
    fields: list[str]
    summary: str
    detail: str
    raised_at: datetime = Field(default_factory=_now)


class ApprovalRequest(BaseModel):
    """The agent has finished and is asking permission to write. No write of any
    kind happens before this is answered."""

    interrupt_type: Literal["APPROVAL"] = "APPROVAL"
    summary: str
    figure_count: int
    finding_count: int
    outstanding_notes: list[str] = Field(default_factory=list)
    raised_at: datetime = Field(default_factory=_now)


class MemoState(BaseModel):
    """Graph state. Nodes return partial updates; LangGraph merges them."""

    application_number: str
    template_id: str | None = None
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)

    documents: list[DocumentRecord] = Field(default_factory=list)

    ledger: dict[str, EvidenceItem] = Field(default_factory=dict)
    unresolved: dict[str, UnresolvedField] = Field(default_factory=dict)
    verifications: list[VerificationResult] = Field(default_factory=list)

    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    draft_sections: dict[str, str] = Field(default_factory=dict)
    review_notes: list[ReviewNote] = Field(default_factory=list)
    revision_count: int = 0

    approved_memo: str | None = None
    approved_by: str | None = None
    rendered_path: str | None = None

    escalation: Escalation | None = None
    approval_request: ApprovalRequest | None = None

    # Free-text trace of what each node did, for the run view in the UI.
    trace: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    # --- ledger operations --------------------------------------------------

    def record(self, field_name: str, value: Any, provenance: Provenance) -> EvidenceItem:
        """Write a resolved value into the ledger and clear any gap against it."""
        item = EvidenceItem(field_name=field_name, value=value, provenance=provenance)
        self.ledger[field_name] = item
        self.unresolved.pop(field_name, None)
        return item

    def note_attempt(self, field_name: str, attempt: Attempt) -> UnresolvedField:
        """Record an unsuccessful attempt against a field's own budget."""
        entry = self.unresolved.get(field_name)
        if entry is None:
            entry = UnresolvedField(field_name=field_name, reason="not_found")
            self.unresolved[field_name] = entry

        # A conflict never reverts to a gap, and never accrues retry attempts.
        if entry.is_conflict:
            return entry

        entry.attempts.append(attempt)
        if attempt.outcome == "parse_failed":
            entry.reason = "parse_failed"
        elif attempt.outcome == "low_confidence":
            entry.reason = "low_confidence"
        return entry

    def record_conflict(self, field_name: str, values: list[EvidenceItem]) -> UnresolvedField:
        """Two sources disagree. Straight to escalation, no retry budget consumed."""
        entry = self.unresolved.get(field_name)
        if entry is None:
            entry = UnresolvedField(field_name=field_name, reason="conflict")
            self.unresolved[field_name] = entry

        entry.reason = "conflict"
        entry.conflicting_values = values
        # A conflicting field must not sit in the ledger as though it were resolved.
        self.ledger.pop(field_name, None)
        return entry

    def attempts_for(self, field_name: str) -> int:
        entry = self.unresolved.get(field_name)
        return len(entry.attempts) if entry else 0

    def budget_exhausted(self, field_name: str, budget: int) -> bool:
        """A conflict never counts as budget exhaustion - it is a different failure."""
        entry = self.unresolved.get(field_name)
        if entry is None or entry.is_conflict:
            return False
        return len(entry.attempts) >= budget

    # --- queries ------------------------------------------------------------

    def missing_fields(self) -> list[str]:
        return [name for name in self.required_fields if name not in self.ledger]

    def is_complete(self) -> bool:
        return not self.missing_fields() and not self.unresolved

    def conflicts(self) -> list[UnresolvedField]:
        return [entry for entry in self.unresolved.values() if entry.is_conflict]

    def documents_of_type(self, document_type: str) -> list[DocumentRecord]:
        return [d for d in self.documents if d.document_type == document_type]

    def figures(self) -> dict[str, Any]:
        """Every value the memo is permitted to state."""
        return {name: item.value for name, item in self.ledger.items()}
