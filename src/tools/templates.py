"""Template selection and the required-field list each template implies.

The evidence agent derives what it must find from a template's required-field
list. A field not declared here will never be sought, and a field declared here
that cannot be sourced becomes an escalation - so this module decides, in effect,
what the system considers a complete file.

Optional fields are sought but their absence is an acceptable omission rather than
an escalation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

LoanPurpose = Literal["owner_occupied", "investment"]
ApplicantStructure = Literal["single", "joint"]

TEMPLATE_IDS = [
    "owner_occupied_single",
    "owner_occupied_joint",
    "investment_single",
    "investment_joint",
]


def select_template(loan_purpose: str, applicant_structure: str) -> str:
    """Choose the memo template. Purely a function of purpose and structure."""
    template_id = f"{loan_purpose}_{applicant_structure}"
    if template_id not in TEMPLATE_IDS:
        raise ValueError(
            f"no template for loan_purpose={loan_purpose!r} "
            f"applicant_structure={applicant_structure!r}"
        )
    return template_id


def borrower_count(template_id: str) -> int:
    return 2 if template_id.endswith("_joint") else 1


def loan_purpose_of(template_id: str) -> LoanPurpose:
    return "investment" if template_id.startswith("investment") else "owner_occupied"


# --- Field declarations -----------------------------------------------------

# Application-level fields sourced from the database.
APPLICATION_DATABASE_FIELDS = [
    "loan_purpose",
    "applicant_structure",
    "security_address",
    "valuation_amount",
    "valuation_date",
    "total_loan_amount",
    "lvr_stated",
    "lodgement_date",
    "loan_splits",
]

# Application-level fields derived by the calculators.
APPLICATION_COMPUTED_FIELDS = [
    "computed_lvr",
    "valuation_age_days",
    "max_interest_only_term_months",
    "computed_total_annual_income",
    "computed_annual_repayments",
    "computed_assessed_repayments",
    "computed_coverage_ratio",
    "computed_annual_surplus",
]

# Per-borrower fields, formatted with the borrower's 1-based position.
BORROWER_RECORD_FIELDS = [
    "borrower_{n}_record_name",
    "borrower_{n}_record_date_of_birth",
    "borrower_{n}_record_address",
]

# Verification fields. Each resolves to a comparison of the record against a
# document, never to one side picked over the other.
BORROWER_VERIFICATION_FIELDS = [
    "borrower_{n}_full_name",
    "borrower_{n}_date_of_birth",
    "borrower_{n}_residential_address",
]

BORROWER_DOCUMENT_FIELDS = [
    "borrower_{n}_credit_score",
    "borrower_{n}_credit_score_band",
    "borrower_{n}_credit_enquiries_6m",
    "borrower_{n}_credit_defaults_count",
    "borrower_{n}_credit_report_date",
    "borrower_{n}_kyc_verification_date",
    "borrower_{n}_kyc_method",
    "borrower_{n}_employer",
]

BORROWER_COMPUTED_FIELDS = [
    "borrower_{n}_payg_income",
]

# Sought, but absence is an omission rather than an escalation.
OPTIONAL_FIELDS = [
    "related_parties",
]

OPTIONAL_BORROWER_FIELDS = [
    "borrower_{n}_kyc_reference",
    "borrower_{n}_credit_accounts",
]


class FieldSpec(BaseModel):
    """What the evidence agent needs to know to go and find a field."""

    field_name: str
    source_kind: Literal["database", "document", "computed", "verification"]
    document_type: Literal["equifax", "kyc", "payslip"] | None = None
    borrower_position: int | None = None
    required: bool = True


def _borrower_specs(position: int) -> list[FieldSpec]:
    specs: list[FieldSpec] = []

    for template in BORROWER_RECORD_FIELDS:
        specs.append(FieldSpec(
            field_name=template.format(n=position),
            source_kind="database",
            borrower_position=position,
        ))

    for template in BORROWER_VERIFICATION_FIELDS:
        # Name and address are verified against the identity record; date of birth
        # against the credit report, which is where the two sources can disagree.
        document_type = "equifax" if template.endswith("date_of_birth") else "kyc"
        specs.append(FieldSpec(
            field_name=template.format(n=position),
            source_kind="verification",
            document_type=document_type,
            borrower_position=position,
        ))

    document_types = {
        "credit_score": "equifax",
        "credit_score_band": "equifax",
        "credit_enquiries_6m": "equifax",
        "credit_defaults_count": "equifax",
        "credit_report_date": "equifax",
        "kyc_verification_date": "kyc",
        "kyc_method": "kyc",
        "employer": "payslip",
    }
    for template in BORROWER_DOCUMENT_FIELDS:
        suffix = template.format(n=position).split(f"borrower_{position}_", 1)[1]
        specs.append(FieldSpec(
            field_name=template.format(n=position),
            source_kind="document",
            document_type=document_types[suffix],
            borrower_position=position,
        ))

    for template in BORROWER_COMPUTED_FIELDS:
        specs.append(FieldSpec(
            field_name=template.format(n=position),
            source_kind="computed",
            document_type="payslip",
            borrower_position=position,
        ))

    optional_types = {"kyc_reference": "kyc", "credit_accounts": "equifax"}
    for template in OPTIONAL_BORROWER_FIELDS:
        suffix = template.format(n=position).split(f"borrower_{position}_", 1)[1]
        specs.append(FieldSpec(
            field_name=template.format(n=position),
            source_kind="document",
            document_type=optional_types[suffix],
            borrower_position=position,
            required=False,
        ))

    return specs


def field_specs(template_id: str) -> list[FieldSpec]:
    """Every field the template needs, with where to look for it."""
    if template_id not in TEMPLATE_IDS:
        raise ValueError(f"unknown template {template_id!r}")

    specs = [
        FieldSpec(field_name=name, source_kind="database")
        for name in APPLICATION_DATABASE_FIELDS
    ]
    specs += [
        FieldSpec(field_name=name, source_kind="computed")
        for name in APPLICATION_COMPUTED_FIELDS
    ]
    specs += [
        FieldSpec(field_name=name, source_kind="database", required=False)
        for name in OPTIONAL_FIELDS
    ]

    for position in range(1, borrower_count(template_id) + 1):
        specs += _borrower_specs(position)

    return specs


def required_fields(template_id: str) -> list[str]:
    return [spec.field_name for spec in field_specs(template_id) if spec.required]


def optional_fields(template_id: str) -> list[str]:
    return [spec.field_name for spec in field_specs(template_id) if not spec.required]


def spec_for(template_id: str, field_name: str) -> FieldSpec | None:
    for spec in field_specs(template_id):
        if spec.field_name == field_name:
            return spec
    return None
