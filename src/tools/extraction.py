"""Document classification and field extraction.

Two implementations of the same protocol:

- `NebiusExtractor` - the production path, structured output from the extraction
  model, as the course brief requires.
- `LocalTableExtractor` - reads the key-value tables directly. It exists so the
  whole graph can run end to end without network access and so the determinism
  and defect tests are exact rather than probabilistic. It is a test double and an
  offline fallback, not a second production path.

Both obey the same contract, and it is the contract that matters:

- every extracted field is optional, and absence returns `None`
- nothing is inferred, completed or guessed from context
- returning `unknown` from classification is a valid, expected outcome

Never make a field non-optional to force a model to produce it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from src.tools.models import logged_call
from src.tools.parsing import ParsedDoc

DocumentType = Literal["equifax", "kyc", "payslip", "unknown"]


# --- Extraction schemas -----------------------------------------------------
# One per document type. Every field is optional, without exception.


class Classification(BaseModel):
    document_type: DocumentType
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


class CreditAccount(BaseModel):
    provider: str | None = None
    account_type: str | None = None
    credit_limit: int | None = None      # cents
    date_opened: date | None = None


class ListedDefault(BaseModel):
    date_listed: date | None = None
    default_type: str | None = None
    amount: int | None = None            # cents
    status: str | None = None


class EquifaxExtraction(BaseModel):
    """A bureau-style credit report."""

    subject_name: str | None = None
    subject_date_of_birth: date | None = None
    subject_address: str | None = None
    report_date: date | None = None
    report_reference: str | None = None
    credit_score: int | None = None
    score_band: str | None = None
    enquiries_last_6_months: int | None = None
    listed_defaults: list[ListedDefault] = Field(default_factory=list)
    credit_accounts: list[CreditAccount] = Field(default_factory=list)


class KycExtraction(BaseModel):
    """An identity verification record."""

    subject_name: str | None = None
    subject_date_of_birth: date | None = None
    subject_address: str | None = None
    verification_method: str | None = None
    verification_date: date | None = None
    verifying_officer: str | None = None
    verification_reference: str | None = None
    outcome: str | None = None


class PayslipExtraction(BaseModel):
    """One PAYG payslip. Money fields are integer cents."""

    employer_name: str | None = None
    employer_abn: str | None = None
    employee_name: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    payment_date: date | None = None
    pay_frequency: str | None = None
    gross_for_period: int | None = None
    tax_withheld: int | None = None
    net_for_period: int | None = None
    superannuation: int | None = None
    ytd_gross: int | None = None
    ytd_tax: int | None = None


SCHEMA_FOR: dict[str, type[BaseModel]] = {
    "equifax": EquifaxExtraction,
    "kyc": KycExtraction,
    "payslip": PayslipExtraction,
}


class ExtractionResult(BaseModel):
    """Never raises into a node. A failure is a value with `ok=False`."""

    document_type: DocumentType
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    extractor: str = ""

    def typed(self) -> BaseModel | None:
        if not self.ok or self.document_type not in SCHEMA_FOR:
            return None
        return SCHEMA_FOR[self.document_type].model_validate(self.data)


@runtime_checkable
class Extractor(Protocol):
    name: str

    def classify(self, doc: ParsedDoc) -> Classification: ...

    def extract(self, doc: ParsedDoc, document_type: DocumentType) -> ExtractionResult: ...


# --- Shared parsing helpers -------------------------------------------------


def parse_cents(value: str | None) -> int | None:
    """Currency text to integer cents. Returns None rather than guessing."""
    if value is None:
        return None
    match = re.search(r"(-?)\$?\s*([\d,]+)(?:\.(\d{1,2}))?", str(value))
    if not match:
        return None
    sign, dollars, cents = match.groups()
    dollars = dollars.replace(",", "")
    if not dollars:
        return None
    total = int(dollars) * 100 + int((cents or "0").ljust(2, "0"))
    return -total if sign else total


def parse_au_date(value: str | None) -> date | None:
    """Dates as these documents write them, or ISO. Returns None on anything else."""
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"-?\d[\d,]*", str(value))
    return int(match.group().replace(",", "")) if match else None


# --- Local, deterministic extractor -----------------------------------------


class LocalTableExtractor:
    """Reads the key-value tables the synthetic documents are built from.

    Deterministic and offline. Used by the tests, and as a fallback when no
    extraction credentials are configured.
    """

    name = "local-table"

    CLASSIFICATION_MARKERS: list[tuple[DocumentType, tuple[str, ...]]] = [
        ("equifax", ("consumer credit report", "credit score", "score band")),
        ("kyc", ("identity verification record", "verification reference", "verifying officer")),
        ("payslip", ("payslip", "pay period end", "year-to-date gross")),
    ]

    def classify(self, doc: ParsedDoc) -> Classification:
        if not doc.ok:
            return Classification(document_type="unknown", confidence=0.0,
                                  reasoning=doc.error or "document could not be parsed")

        haystack = doc.text.lower()
        scores: dict[DocumentType, float] = {}
        for document_type, markers in self.CLASSIFICATION_MARKERS:
            hits = sum(1 for marker in markers if marker in haystack)
            if hits:
                scores[document_type] = hits / len(markers)

        if not scores:
            return Classification(document_type="unknown", confidence=0.0,
                                  reasoning="no marker for any known document type")

        best = max(scores, key=scores.get)
        return Classification(
            document_type=best,
            confidence=round(scores[best], 2),
            reasoning=f"matched {int(scores[best] * 100)}% of the {best} markers",
        )

    def extract(self, doc: ParsedDoc, document_type: DocumentType) -> ExtractionResult:
        if not doc.ok:
            return ExtractionResult(document_type=document_type, ok=False,
                                    error=doc.error, extractor=self.name)
        if document_type not in SCHEMA_FOR:
            return ExtractionResult(document_type=document_type, ok=False,
                                    error=f"no schema for document type {document_type!r}",
                                    extractor=self.name)

        pairs = doc.pairs()
        builder = {
            "equifax": self._equifax,
            "kyc": self._kyc,
            "payslip": self._payslip,
        }[document_type]

        try:
            model = builder(pairs, doc)
        except Exception as exc:  # noqa: BLE001 - a failure is a value, not an escape
            return ExtractionResult(document_type=document_type, ok=False,
                                    error=f"{type(exc).__name__}: {exc}", extractor=self.name)

        return ExtractionResult(
            document_type=document_type,
            ok=True,
            data=model.model_dump(mode="json"),
            extractor=self.name,
        )

    def _equifax(self, pairs: dict[str, str], doc: ParsedDoc) -> EquifaxExtraction:
        defaults: list[ListedDefault] = []
        accounts: list[CreditAccount] = []

        for page in doc.pages:
            for table in page.clean_tables():
                header = [cell.lower() for cell in table[0]] if table else []
                if header[:2] == ["date listed", "type"]:
                    for cells in table[1:]:
                        defaults.append(ListedDefault(
                            date_listed=parse_au_date(cells[0]),
                            default_type=cells[1] or None,
                            amount=parse_cents(cells[2]),
                            status=cells[3] or None,
                        ))
                elif header[:2] == ["provider", "account type"]:
                    for cells in table[1:]:
                        accounts.append(CreditAccount(
                            provider=cells[0] or None,
                            account_type=cells[1] or None,
                            credit_limit=parse_cents(cells[2]),
                            date_opened=parse_au_date(cells[3]),
                        ))

        return EquifaxExtraction(
            subject_name=pairs.get("Subject name"),
            subject_date_of_birth=parse_au_date(pairs.get("Date of birth")),
            subject_address=pairs.get("Residential address"),
            report_date=parse_au_date(pairs.get("Report date")),
            report_reference=pairs.get("Report reference"),
            credit_score=parse_int(pairs.get("Credit score")),
            score_band=pairs.get("Score band"),
            enquiries_last_6_months=parse_int(pairs.get("Enquiries in last 6 months")),
            listed_defaults=defaults,
            credit_accounts=accounts,
        )

    def _kyc(self, pairs: dict[str, str], doc: ParsedDoc) -> KycExtraction:
        return KycExtraction(
            subject_name=pairs.get("Full name"),
            subject_date_of_birth=parse_au_date(pairs.get("Date of birth")),
            subject_address=pairs.get("Residential address"),
            verification_method=pairs.get("Verification method"),
            verification_date=parse_au_date(pairs.get("Verification date")),
            verifying_officer=pairs.get("Verifying officer"),
            verification_reference=pairs.get("Verification reference"),
            outcome=pairs.get("Outcome"),
        )

    def _payslip(self, pairs: dict[str, str], doc: ParsedDoc) -> PayslipExtraction:
        return PayslipExtraction(
            employer_name=pairs.get("Employer name"),
            employer_abn=pairs.get("ABN"),
            employee_name=pairs.get("Employee name"),
            period_start=parse_au_date(pairs.get("Pay period start")),
            period_end=parse_au_date(pairs.get("Pay period end")),
            payment_date=parse_au_date(pairs.get("Payment date")),
            pay_frequency=(pairs.get("Pay frequency") or "").lower() or None,
            gross_for_period=parse_cents(pairs.get("Gross for period")),
            tax_withheld=parse_cents(pairs.get("Tax withheld")),
            net_for_period=parse_cents(pairs.get("Net for period")),
            superannuation=parse_cents(pairs.get("Superannuation")),
            ytd_gross=parse_cents(pairs.get("Year-to-date gross")),
            ytd_tax=parse_cents(pairs.get("Year-to-date tax")),
        )


# --- Nebius extractor -------------------------------------------------------


CLASSIFY_PROMPT = """You are classifying one document from a loan application file.

Reply with exactly one of these document types:

- equifax: a consumer credit report from a credit bureau - credit score, enquiries,
  listed defaults, credit accounts
- kyc: an identity verification record - a subject's identity confirmed against
  identification documents by a verifying officer
- payslip: a PAYG payslip - an employer paying an employee for a pay period
- unknown: anything else, or a document you cannot confidently place

`unknown` is a correct and expected answer. Do not force a document into one of the
other three types. Classify only from the content below.

DOCUMENT
--------
{text}
"""

EXTRACT_PROMPT = """Extract the listed fields from this {document_type} document.

Rules you must follow:

- Return null for any field that does not appear in the document. Never infer a
  value, never complete a partial one, and never carry a value over from your
  general knowledge.
- Copy values exactly as written. Do not reformat names or correct apparent
  spelling.
- Money fields are integer cents: $4,150.00 is 415000.
- Dates are ISO format. The document writes dates day first: 09/01/2026 is
  2026-01-09.

DOCUMENT
--------
{text}
"""


class NebiusExtractor:
    """The production path. Structured output only, temperature zero, every call logged."""

    name = "nebius"
    provider = "nebius"

    def __init__(self, model=None, confidence_floor: float = 0.5) -> None:
        from config import EXTRACTION_MODEL
        from src.tools.models import extraction_model

        self._model = model or extraction_model()
        self.model_id = EXTRACTION_MODEL
        self.confidence_floor = confidence_floor

    def classify(self, doc: ParsedDoc) -> Classification:
        if not doc.ok:
            return Classification(document_type="unknown", confidence=0.0,
                                  reasoning=doc.error or "document could not be parsed")

        prompt = CLASSIFY_PROMPT.format(text=doc.text[:6000])
        structured = self._model.with_structured_output(Classification)

        try:
            result = logged_call(
                purpose="classify_document",
                provider=self.provider,
                model=self.model_id,
                prompt=prompt,
                invoke=lambda: structured.invoke(prompt),
            )
        except Exception as exc:  # noqa: BLE001
            return Classification(document_type="unknown", confidence=0.0,
                                  reasoning=f"classification call failed: {exc}")

        if result.confidence < self.confidence_floor:
            return Classification(
                document_type="unknown",
                confidence=result.confidence,
                reasoning=(
                    f"model proposed {result.document_type} at confidence "
                    f"{result.confidence}, below the floor of {self.confidence_floor}"
                ),
            )
        return result

    def extract(self, doc: ParsedDoc, document_type: DocumentType) -> ExtractionResult:
        if not doc.ok:
            return ExtractionResult(document_type=document_type, ok=False,
                                    error=doc.error, extractor=self.name)
        if document_type not in SCHEMA_FOR:
            return ExtractionResult(document_type=document_type, ok=False,
                                    error=f"no schema for document type {document_type!r}",
                                    extractor=self.name)

        schema = SCHEMA_FOR[document_type]
        prompt = EXTRACT_PROMPT.format(document_type=document_type, text=doc.text[:12000])
        structured = self._model.with_structured_output(schema)

        try:
            result = logged_call(
                purpose=f"extract_{document_type}",
                provider=self.provider,
                model=self.model_id,
                prompt=prompt,
                invoke=lambda: structured.invoke(prompt),
            )
        except Exception as exc:  # noqa: BLE001
            return ExtractionResult(document_type=document_type, ok=False,
                                    error=f"{type(exc).__name__}: {exc}", extractor=self.name)

        return ExtractionResult(
            document_type=document_type,
            ok=True,
            data=result.model_dump(mode="json"),
            extractor=self.name,
        )


def default_extractor() -> Extractor:
    """Nebius when it is configured, the local extractor otherwise.

    The fallback keeps the graph runnable offline. It is reported in the run trace
    so no one mistakes an offline run for a production one.
    """
    from config import NEBIUS_API_KEY

    if NEBIUS_API_KEY:
        return NebiusExtractor()
    return LocalTableExtractor()
