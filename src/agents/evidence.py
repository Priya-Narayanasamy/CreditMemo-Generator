"""The evidence agent - the only genuinely agentic node.

The loop:

1. derive the required fields from the selected template
2. check the ledger for gaps
3. choose which source to try next for one unresolved field
4. execute - a database query, or classify then parse then extract on a document
5. write the result or the failure into the ledger
6. exit when the evidence is complete, or escalate on an exhausted per-field
   budget or a detected conflict

Step 3 is the model's only job here, and it is narrow enough to test: given an
unresolved field and the sources available, which source plausibly contains it.
Everything else is deterministic.

Identity verification belongs to this agent. Name, date of birth and address in
the credit report and the identity record are compared against the borrower
record. A mismatch is a conflict, not a gap - it escalates immediately and
consumes no retry budget.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from config import MAX_ATTEMPTS_PER_FIELD, POLICY_PATH
from src.state import (
    Attempt,
    DocumentRecord,
    Escalation,
    EvidenceItem,
    MemoState,
    Provenance,
    VerificationResult,
)
from src.tools import database as db
from src.tools.calculators import (
    LoanSplitTerms,
    PayslipFigures,
    annualise_payg_income,
    calculate_lvr,
    calculate_serviceability,
    days_between,
    proposed_annual_repayments,
)
from src.tools.documents import DocumentRef, list_documents
from src.tools.extraction import (
    Classification,
    Extractor,
    PayslipExtraction,
    default_extractor,
)
from src.tools.parsing import DEFAULT_PARSER, ParsedDoc, parser_for_attempt
from src.tools.policy import load_ruleset
from src.tools.templates import (
    field_specs,
    optional_fields,
    required_fields,
    select_template,
)


# --- Normalisation used by every comparison ---------------------------------


def normalise_name(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def normalise_address(value: str) -> str:
    text = re.sub(r"[.,/]", " ", (value or "").casefold())
    return re.sub(r"\s+", " ", text).strip()


# --- A document, parsed, classified and extracted once ----------------------


class EvidenceDocument(BaseModel):
    """One document and everything derived from it, cached.

    Parsing and extraction happen once per document. Re-extracting the same file
    for every field would be wasteful and, worse, would let the same document
    produce two different answers within one run.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ref: DocumentRef
    parsed: ParsedDoc
    classification: Classification
    data: dict | None = None
    extraction_error: str | None = None
    borrower_position: int | None = None
    match_score: int = 0

    @property
    def document_type(self) -> str:
        return self.classification.document_type

    @property
    def readable(self) -> bool:
        return self.parsed.ok

    @property
    def usable(self) -> bool:
        return self.parsed.ok and self.document_type != "unknown" and self.data is not None

    @property
    def subject_name(self) -> str | None:
        if not self.data:
            return None
        return self.data.get("subject_name") or self.data.get("employee_name")


# --- Source selection -------------------------------------------------------


class SourceCandidate(BaseModel):
    """One place a field might be found."""

    ref: str
    kind: str = "document"
    document_type: str | None = None
    subject_name: str | None = None
    readable: bool = True

    def describe(self) -> str:
        subject = f", subject {self.subject_name}" if self.subject_name else ""
        state = "" if self.readable else ", previously failed to parse"
        return f"{self.ref} ({self.document_type or self.kind}{subject}{state})"


class SourceSelector(Protocol):
    name: str

    def select(self, field_name: str, candidates: list[SourceCandidate]) -> SourceCandidate | None:
        ...


class RuleSourceSelector:
    """Deterministic selection, preferring a readable document of the right type.

    Used offline and in tests, and as the fallback when the model selector cannot
    answer. It is not a lesser answer - the mapping from field to document type is
    known for these documents, which is why this system uses targeted extraction
    rather than retrieval.
    """

    name = "rule"

    def __init__(self, expected_type: str | None = None, subject_name: str | None = None) -> None:
        self.expected_type = expected_type
        self.subject_name = subject_name

    def select(self, field_name: str, candidates: list[SourceCandidate]) -> SourceCandidate | None:
        if not candidates:
            return None

        readable = [c for c in candidates if c.readable]
        matching = [
            c for c in readable
            if self.expected_type is None or c.document_type == self.expected_type
        ]

        if self.subject_name:
            same_subject = [c for c in matching if c.subject_name == self.subject_name]
            if same_subject:
                return same_subject[0]

        if matching:
            return matching[0]

        # Nothing readable holds the field. An unreadable document is the next most
        # plausible place for it, and trying it is what turns a silent gap into a
        # reported parse failure.
        unreadable = [c for c in candidates if not c.readable]
        return unreadable[0] if unreadable else None


SELECT_SOURCE_PROMPT = """You are choosing which document to read next to find one
field of a loan application.

FIELD: {field_name}

CANDIDATE SOURCES:
{candidates}

Reply with the number of the single source most likely to contain that field. If
none of them plausibly contains it, reply with 0. Do not explain.
"""


class ModelSourceSelector:
    """The model's one job in this loop.

    Falls back to the rule selector on an unusable answer, so a bad response
    degrades the run rather than breaking it.
    """

    name = "model"

    def __init__(self, model=None, fallback: SourceSelector | None = None) -> None:
        from config import EXTRACTION_MODEL
        from src.tools.models import extraction_model

        self._model = model or extraction_model()
        self.model_id = EXTRACTION_MODEL
        self.fallback = fallback or RuleSourceSelector()

    def select(self, field_name: str, candidates: list[SourceCandidate]) -> SourceCandidate | None:
        if not candidates:
            return None

        from src.tools.models import logged_call

        listing = "\n".join(
            f"{index}. {candidate.describe()}" for index, candidate in enumerate(candidates, 1)
        )
        prompt = SELECT_SOURCE_PROMPT.format(field_name=field_name, candidates=listing)

        try:
            response = logged_call(
                purpose="select_source",
                provider="nebius",
                model=self.model_id,
                prompt=prompt,
                invoke=lambda: self._model.invoke(prompt),
            )
            text = getattr(response, "content", str(response))
            match = re.search(r"\d+", str(text))
            choice = int(match.group()) if match else -1
        except Exception:  # noqa: BLE001 - degrade to the rule selector
            return self.fallback.select(field_name, candidates)

        if choice == 0:
            return None
        if 1 <= choice <= len(candidates):
            return candidates[choice - 1]
        return self.fallback.select(field_name, candidates)


# --- Identity comparison ----------------------------------------------------


def compare_values(
    field_name: str, record: EvidenceItem | None, document: EvidenceItem | None
) -> VerificationResult:
    """Compare a record value against a document value. A pure function.

    One side missing is `not_present`, not a conflict - absence is a gap and
    disagreement is a conflict, and the two escalate differently.
    """
    if record is None or document is None:
        return VerificationResult(
            field_name=field_name,
            status="not_present",
            record_value=record,
            document_value=document,
        )

    left, right = record.value, document.value

    if isinstance(left, date) or isinstance(right, date):
        equal = str(left) == str(right)
    elif "address" in field_name:
        equal = normalise_address(str(left)) == normalise_address(str(right))
    else:
        equal = normalise_name(str(left)) == normalise_name(str(right))

    return VerificationResult(
        field_name=field_name,
        status="match" if equal else "conflict",
        record_value=record,
        document_value=document,
    )


# --- The agent --------------------------------------------------------------


class EvidenceAgent:
    # Which extracted key holds each per-borrower field.
    FIELD_KEYS = {
        "credit_score": "credit_score",
        "credit_score_band": "score_band",
        "credit_enquiries_6m": "enquiries_last_6_months",
        "credit_report_date": "report_date",
        "kyc_verification_date": "verification_date",
        "kyc_method": "verification_method",
        "kyc_reference": "verification_reference",
        "employer": "employer_name",
    }

    # Which document, and which key in it, verifies each identity field.
    IDENTITY_CHECKS = [
        ("full_name", "record_name", "kyc", "subject_name"),
        ("date_of_birth", "record_date_of_birth", "equifax", "subject_date_of_birth"),
        ("residential_address", "record_address", "kyc", "subject_address"),
    ]

    SERVICEABILITY_FIELDS = [
        "computed_total_annual_income",
        "computed_annual_repayments",
        "computed_assessed_repayments",
        "computed_coverage_ratio",
        "computed_annual_surplus",
    ]

    def __init__(
        self,
        extractor: Extractor | None = None,
        selector_factory=None,
        max_attempts: int = MAX_ATTEMPTS_PER_FIELD,
    ) -> None:
        self.extractor = extractor or default_extractor()
        self.selector_factory = selector_factory or (
            lambda expected_type, subject: RuleSourceSelector(expected_type, subject)
        )
        self.max_attempts = max_attempts
        self.ruleset = load_ruleset(POLICY_PATH)

    # -- step 1: the record --------------------------------------------------

    def load_record(self, state: MemoState) -> db.ApplicationFile | None:
        file = db.get_application_file(state.application_number)
        if file is None:
            state.escalation = Escalation(
                fields=["application_number"],
                summary=f"No application {state.application_number} in the database.",
                detail=(
                    f"A memo was requested for {state.application_number}, which does "
                    f"not exist in the applications table. Nothing was drafted."
                ),
            )
            return None

        application = file.application
        state.template_id = select_template(
            application.loan_purpose, application.applicant_structure
        )
        state.required_fields = required_fields(state.template_id)
        state.optional_fields = optional_fields(state.template_id)

        for column in (
            "loan_purpose", "applicant_structure", "security_address",
            "valuation_amount", "valuation_date", "total_loan_amount",
            "lvr_stated", "lodgement_date",
        ):
            state.record(column, getattr(application, column), application.provenance_for(column))

        state.record(
            "loan_splits",
            [s.model_dump(mode="json") for s in file.loan_splits],
            Provenance(source_kind="database",
                       detail={"table": "loan_splits", "column": "*",
                               "row_key": state.application_number}),
        )

        if file.related_parties:
            state.record(
                "related_parties",
                [p.model_dump(mode="json") for p in file.related_parties],
                Provenance(source_kind="database",
                           detail={"table": "related_parties", "column": "*",
                                   "row_key": state.application_number}),
            )

        for position, borrower in enumerate(file.ordered_borrowers(), start=1):
            for column, suffix in (
                ("full_name", "record_name"),
                ("date_of_birth", "record_date_of_birth"),
                ("residential_address", "record_address"),
            ):
                state.record(
                    f"borrower_{position}_{suffix}",
                    getattr(borrower, column),
                    borrower.provenance_for(column),
                )

        state.trace.append(
            f"Loaded {state.application_number} from the database; template "
            f"{state.template_id}, {len(state.required_fields)} required fields."
        )
        return file

    # -- step 2: the documents -----------------------------------------------

    def load_documents(
        self, state: MemoState, file: db.ApplicationFile, attempt_index: int = 0
    ) -> list[EvidenceDocument]:
        parser = parser_for_attempt(attempt_index)
        documents: list[EvidenceDocument] = []

        for ref in list_documents(state.application_number):
            parsed = parser.parse(ref.path)
            classification = self.extractor.classify(parsed)

            document = EvidenceDocument(ref=ref, parsed=parsed, classification=classification)

            if parsed.ok and classification.document_type != "unknown":
                result = self.extractor.extract(parsed, classification.document_type)
                if result.ok:
                    document.data = result.data
                else:
                    document.extraction_error = result.error

            documents.append(document)

        self.assign_borrowers(documents, file.ordered_borrowers())

        for document in documents:
            state.documents.append(DocumentRecord(
                filename=document.ref.filename,
                path=document.ref.path,
                document_type=document.document_type,
                classification_confidence=document.classification.confidence,
                page_count=document.parsed.page_count or None,
                parse_ok=document.parsed.ok,
                parse_error=document.parsed.error,
                subject_name=document.subject_name,
            ))

        usable = sum(1 for d in documents if d.usable)
        unreadable = [d.ref.filename for d in documents if not d.readable]
        state.trace.append(
            f"Found {len(documents)} documents; {usable} classified and extracted"
            + (f", unreadable: {', '.join(unreadable)}" if unreadable else "")
            + "."
        )
        return documents

    # -- whose document is this ----------------------------------------------

    @staticmethod
    def match_score(data: dict, borrower: db.Borrower) -> int:
        """How strongly a document's subject matches a borrower row.

        Never assumes the names agree - the whole point of verification is that
        they might not. An exact name match is decisive; otherwise a shared
        surname, date of birth or address builds the case.
        """
        subject = data.get("subject_name") or data.get("employee_name") or ""
        score = 0

        if normalise_name(subject) == normalise_name(borrower.full_name):
            score += 100
        elif subject and borrower.full_name.split()[-1].casefold() in subject.casefold():
            score += 10

        dob = data.get("subject_date_of_birth")
        if dob and str(dob) == borrower.date_of_birth.isoformat():
            score += 30

        address = data.get("subject_address")
        if address and normalise_address(address) == normalise_address(borrower.residential_address):
            score += 20

        return score

    def assign_borrowers(
        self, documents: list[EvidenceDocument], borrowers: list[db.Borrower]
    ) -> None:
        """Attach each document to the borrower it most plausibly belongs to.

        Done once, up front. A document that matches two borrowers equally well -
        which is what happens when joint applicants share a surname - is left
        unassigned rather than guessed at, because a wrong assignment manufactures
        a conflict that does not exist.
        """
        for document in documents:
            if not document.usable:
                continue

            scores = [(self.match_score(document.data, b), position)
                      for position, b in enumerate(borrowers, start=1)]
            scores.sort(reverse=True)

            best_score, best_position = scores[0]
            if best_score == 0:
                continue
            if len(scores) > 1 and scores[1][0] == best_score:
                continue

            document.borrower_position = best_position
            document.match_score = best_score

    # -- step 3-5: the loop --------------------------------------------------

    def resolve_fields(
        self, state: MemoState, file: db.ApplicationFile, documents: list[EvidenceDocument]
    ) -> None:
        borrowers = file.ordered_borrowers()

        for spec in field_specs(state.template_id):
            if spec.source_kind != "document" or spec.field_name in state.ledger:
                continue

            borrower = borrowers[spec.borrower_position - 1]
            self._resolve_one(state, spec, borrower, documents)

        self._resolve_income(state, documents, borrowers)

    def _resolve_one(self, state: MemoState, spec, borrower, documents) -> None:
        selector = self.selector_factory(spec.document_type, borrower.full_name)
        tried: set[str] = set()

        while not state.budget_exhausted(spec.field_name, self.max_attempts):
            candidates = self._candidates(documents, spec, borrower, tried)
            choice = selector.select(spec.field_name, candidates)

            if choice is None:
                state.note_attempt(spec.field_name, Attempt(
                    source_kind="document",
                    source_ref=f"{spec.document_type} documents for {borrower.full_name}",
                    outcome="not_present",
                    detail=(
                        f"no {spec.document_type} document for {borrower.full_name} "
                        f"remained to try"
                    ),
                ))
                return

            tried.add(choice.ref)
            document = next(d for d in documents if d.ref.filename == choice.ref)

            if not document.readable:
                state.note_attempt(spec.field_name, Attempt(
                    source_kind="document",
                    source_ref=choice.ref,
                    outcome="parse_failed",
                    detail=(
                        f"{document.parsed.error}. This document could not be read, so "
                        f"whether it holds {spec.field_name} is unknown"
                    ),
                ))
                continue

            if document.data is None:
                state.note_attempt(spec.field_name, Attempt(
                    source_kind="document",
                    source_ref=choice.ref,
                    outcome="extraction_failed",
                    detail=document.extraction_error or "extraction returned no data",
                ))
                continue

            value = self._field_from(spec.field_name, spec.borrower_position, document.data)
            if value is None:
                state.note_attempt(spec.field_name, Attempt(
                    source_kind="document",
                    source_ref=choice.ref,
                    outcome="not_present",
                    detail=(
                        f"parsed cleanly as a {document.document_type} document for "
                        f"{document.subject_name or 'an unidentified subject'}; the "
                        f"field is not in it"
                    ),
                ))
                continue

            state.record(
                spec.field_name,
                value,
                document.ref.provenance(
                    document.document_type,
                    page=document.parsed.page_of(str(value)) or 1,
                ),
            )
            return

    def _candidates(self, documents, spec, borrower, tried: set[str]) -> list[SourceCandidate]:
        candidates = []
        for document in documents:
            if document.ref.filename in tried:
                continue

            if document.readable:
                if document.document_type != spec.document_type:
                    continue
                if (
                    document.borrower_position is not None
                    and document.borrower_position != spec.borrower_position
                ):
                    continue
            # An unreadable document keeps its place in the list: its type and
            # subject are unknown, so it might be the very document being sought.

            candidates.append(SourceCandidate(
                ref=document.ref.filename,
                document_type=document.document_type,
                subject_name=document.subject_name,
                readable=document.readable,
            ))
        return candidates

    def _field_from(self, field_name: str, position: int, data: dict):
        suffix = field_name.split(f"borrower_{position}_", 1)[1]

        if suffix == "credit_defaults_count":
            return len(data.get("listed_defaults") or [])
        if suffix == "credit_accounts":
            return data.get("credit_accounts") or None

        return data.get(self.FIELD_KEYS.get(suffix, suffix))

    # -- income --------------------------------------------------------------

    def _resolve_income(self, state, documents, borrowers) -> None:
        for position, borrower in enumerate(borrowers, start=1):
            field_name = f"borrower_{position}_payg_income"
            if field_name in state.ledger:
                continue

            slips: list[PayslipFigures] = []
            for document in documents:
                if not document.usable or document.document_type != "payslip":
                    continue
                if document.borrower_position != position:
                    continue

                payslip = PayslipExtraction.model_validate(document.data)
                if payslip.gross_for_period is None or payslip.period_end is None:
                    state.note_attempt(field_name, Attempt(
                        source_kind="document", source_ref=document.ref.filename,
                        outcome="not_present",
                        detail="payslip parsed cleanly but states no period gross",
                    ))
                    continue

                slips.append(PayslipFigures(
                    source_filename=document.ref.filename,
                    period_start=payslip.period_start or payslip.period_end,
                    period_end=payslip.period_end,
                    gross_for_period=payslip.gross_for_period,
                    ytd_gross=payslip.ytd_gross,
                    pay_frequency=(payslip.pay_frequency or "fortnightly").lower(),
                ))

            result = annualise_payg_income(slips)

            if result.status == "resolved":
                state.record(field_name, result.annual_gross, Provenance(
                    source_kind="computed",
                    detail={
                        "function": "annualise_payg_income",
                        "inputs": result.source_filenames,
                        "period_gross": result.period_gross,
                        "periods_per_year": result.periods_per_year,
                    },
                ))
            elif result.status == "conflict":
                state.record_conflict(field_name, [
                    EvidenceItem(
                        field_name=field_name,
                        value=conflict.description,
                        provenance=Provenance(
                            source_kind="document",
                            detail={"filename": ", ".join(conflict.sources),
                                    "page": None, "document_type": "payslip"},
                        ),
                    )
                    for conflict in result.conflicts
                ])
            else:
                state.note_attempt(field_name, Attempt(
                    source_kind="document",
                    source_ref=f"payslips for {borrower.full_name}",
                    outcome="not_present",
                    detail=result.detail,
                ))

    # -- identity verification -----------------------------------------------

    def verify_identity(self, state: MemoState, file: db.ApplicationFile, documents) -> None:
        for position, borrower in enumerate(file.ordered_borrowers(), start=1):
            for suffix, record_field, document_type, document_key in self.IDENTITY_CHECKS:
                field_name = f"borrower_{position}_{suffix}"
                record_item = state.ledger.get(f"borrower_{position}_{record_field}")
                document_item = self._document_identity(
                    documents, position, document_type, document_key, field_name
                )

                result = compare_values(field_name, record_item, document_item)
                state.verifications.append(result)

                if result.status == "match":
                    state.record(field_name, record_item.value, record_item.provenance)
                elif result.status == "conflict":
                    state.record_conflict(field_name, [record_item, document_item])
                else:
                    unreadable = [d.ref.filename for d in documents if not d.readable]
                    state.note_attempt(field_name, Attempt(
                        source_kind="document",
                        source_ref=f"{document_type} for {borrower.full_name}",
                        outcome="parse_failed" if unreadable else "not_present",
                        detail=(
                            f"no readable {document_type} document could be matched to "
                            f"{borrower.full_name}, so {suffix.replace('_', ' ')} could "
                            f"not be verified against the record"
                            + (f" (unreadable: {', '.join(unreadable)})" if unreadable else "")
                        ),
                    ))

    def _document_identity(
        self, documents, position: int, document_type: str, key: str, field_name: str
    ) -> EvidenceItem | None:
        for document in documents:
            if not document.usable or document.document_type != document_type:
                continue
            if document.borrower_position != position:
                continue

            value = document.data.get(key)
            if value is None:
                continue

            return EvidenceItem(
                field_name=field_name,
                value=value,
                provenance=document.ref.provenance(document_type, page=1),
            )
        return None

    # -- computed fields -----------------------------------------------------

    def compute_derived(self, state: MemoState, file: db.ApplicationFile) -> None:
        application = file.application

        lvr = calculate_lvr(application.total_loan_amount, application.valuation_amount)
        lvr_provenance = Provenance(
            source_kind="computed",
            detail={"function": "calculate_lvr",
                    "inputs": ["total_loan_amount", "valuation_amount"]},
        )

        # The system of record states its own LVR. Two sources for one fact, so a
        # disagreement is a conflict for the analyst, not a figure to pick between.
        if abs(float(lvr) - application.lvr_stated) > 0.0001:
            state.record_conflict("computed_lvr", [
                EvidenceItem(field_name="computed_lvr", value=str(lvr),
                             provenance=lvr_provenance),
                EvidenceItem(field_name="computed_lvr", value=f"{application.lvr_stated:.4f}",
                             provenance=application.provenance_for("lvr_stated")),
            ])
        else:
            state.record("computed_lvr", str(lvr), lvr_provenance)

        state.record(
            "valuation_age_days",
            days_between(application.valuation_date, application.lodgement_date),
            Provenance(source_kind="computed",
                       detail={"function": "days_between",
                               "inputs": ["valuation_date", "lodgement_date"]}),
        )

        interest_only = [
            s.term_months for s in file.loan_splits if s.product_type == "interest_only"
        ]
        state.record("max_interest_only_term_months", max(interest_only, default=0), Provenance(
            source_kind="computed",
            detail={"function": "max_interest_only_term_months", "inputs": ["loan_splits"]},
        ))

        expected_incomes = [
            f"borrower_{position}_payg_income"
            for position in range(1, len(file.ordered_borrowers()) + 1)
        ]
        missing_income = [name for name in expected_incomes if name not in state.ledger]

        if missing_income:
            # Serviceability from a partial income picture would be a figure that
            # looks sourced and is not. Better to have none.
            for name in self.SERVICEABILITY_FIELDS:
                state.note_attempt(name, Attempt(
                    source_kind="computed",
                    source_ref="calculate_serviceability",
                    outcome="not_present",
                    detail=(
                        "income is unresolved for " + ", ".join(missing_income)
                        + "; serviceability was not computed from a partial income picture"
                    ),
                ))
            return

        total_income = sum(state.ledger[name].value for name in expected_incomes)
        state.record("computed_total_annual_income", total_income, Provenance(
            source_kind="computed",
            detail={"function": "sum_cents", "inputs": sorted(expected_incomes)},
        ))

        splits = [
            LoanSplitTerms(split_number=s.split_number, amount=s.amount,
                           product_type=s.product_type, term_months=s.term_months)
            for s in file.loan_splits
        ]
        repayments = proposed_annual_repayments(splits, self.ruleset.decimal_rates)
        state.record("computed_annual_repayments", repayments, Provenance(
            source_kind="computed",
            detail={"function": "proposed_annual_repayments", "inputs": ["loan_splits"],
                    "policy_version": self.ruleset.version},
        ))

        serviceability = calculate_serviceability(
            gross_annual_income=total_income,
            existing_commitments=0,
            proposed_repayments=repayments,
            assessment_rate_buffer=self.ruleset.buffer,
        )

        inputs = ["computed_total_annual_income", "computed_annual_repayments"]
        state.record("computed_assessed_repayments", serviceability.assessed_annual_repayments,
                     Provenance(source_kind="computed",
                                detail={"function": "calculate_serviceability", "inputs": inputs,
                                        "assessment_rate_buffer": str(self.ruleset.buffer)}))
        state.record("computed_annual_surplus", serviceability.annual_surplus,
                     Provenance(source_kind="computed",
                                detail={"function": "calculate_serviceability", "inputs": inputs}))
        state.record("computed_coverage_ratio",
                     str(serviceability.coverage_ratio) if serviceability.coverage_ratio else None,
                     Provenance(source_kind="computed",
                                detail={"function": "calculate_serviceability", "inputs": inputs}))

    # -- escalation ----------------------------------------------------------

    def build_escalation(self, state: MemoState) -> Escalation | None:
        blocking = {
            name: entry for name, entry in state.unresolved.items()
            if name in state.required_fields
        }
        if not blocking:
            return None

        conflicts = sorted(name for name, entry in blocking.items() if entry.is_conflict)
        gaps = sorted(name for name in blocking if name not in conflicts)

        parts = []
        if conflicts:
            parts.append(
                f"{len(conflicts)} field(s) where sources disagree: " + ", ".join(conflicts)
            )
        if gaps:
            parts.append(f"{len(gaps)} field(s) not found: " + ", ".join(gaps))

        detail = "\n\n".join(blocking[name].describe() for name in conflicts + gaps)

        return Escalation(
            fields=conflicts + gaps,
            summary="; ".join(parts),
            detail=(
                f"{state.application_number} cannot be drafted as it stands.\n\n{detail}\n\n"
                "Supply the missing values or correct the record, then resume. "
                "Nothing has been written."
            ),
        )


# --- Graph node -------------------------------------------------------------


def evidence_node(state: MemoState, agent: EvidenceAgent | None = None) -> dict:
    """Run the evidence loop over one application."""
    agent = agent or EvidenceAgent()

    file = agent.load_record(state)
    if file is None:
        return {"escalation": state.escalation, "trace": state.trace}

    documents = agent.load_documents(state, file)
    agent.resolve_fields(state, file, documents)
    agent.verify_identity(state, file, documents)
    agent.compute_derived(state, file)

    escalation = agent.build_escalation(state)
    state.trace.append(
        f"Evidence loop finished: {len(state.ledger)} fields resolved, "
        f"{len(state.unresolved)} unresolved."
    )

    return {
        "template_id": state.template_id,
        "required_fields": state.required_fields,
        "optional_fields": state.optional_fields,
        "documents": state.documents,
        "ledger": state.ledger,
        "unresolved": state.unresolved,
        "verifications": state.verifications,
        "escalation": escalation,
        "trace": state.trace,
    }
