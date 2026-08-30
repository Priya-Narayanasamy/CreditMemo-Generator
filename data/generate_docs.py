"""Generate the synthetic supporting documents for each application.

Every document is a text-extractable PDF with real table structure, watermarked
`SAMPLE - SYNTHETIC DATA`. Nothing here imitates a real institution: the credit
report is structurally similar to an Australian bureau report but carries no real
branding, trade dress or document numbers.

Filenames are deliberately unhelpful and decorrelated from document type - the
agent must classify from content. The filename assigned to a document is a pure
function of the application number and the document's identity, so the mapping is
stable across regenerations without being guessable from the type.

Deterministic: no randomness anywhere. Seeded defects are written by
`data/generate_db.py` (defects 4, 6, 8 originate in the data) and by the
`DEFECTS` table below (defects 3, 5, 7 change what is generated here).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_CENTER  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from config import DEFECTS_PATH, DOCUMENTS_DIR, WATERMARK_TEXT  # noqa: E402
from generate_db import APPLICATIONS  # noqa: E402

# --- Seeded defects ---------------------------------------------------------
# Exactly one thing is broken per application. Defects 4, 6 and 8 are differences
# between the database and a document, so the document side is described here and
# the database side lives in generate_db.py.

DEFECTS = [
    {
        "application_number": "APP-2026-0001",
        "category": "clean",
        "description": "Happy path. Single owner-occupied applicant, all documents present and consistent.",
        "affected_fields": [],
        "expected_outcome": "approval",
        "expected_reason": None,
    },
    {
        "application_number": "APP-2026-0002",
        "category": "clean",
        "description": "Happy path. Joint owner-occupied applicants, both fully documented.",
        "affected_fields": [],
        "expected_outcome": "approval",
        "expected_reason": None,
    },
    {
        "application_number": "APP-2026-0003",
        "category": "missing",
        "description": "No payslips generated for the second applicant, Nathan Halloway.",
        "affected_fields": ["borrower_2_payg_income"],
        "expected_outcome": "escalation",
        "expected_reason": "not_found",
    },
    {
        "application_number": "APP-2026-0004",
        "category": "conflict",
        "description": "KYC document names the subject Katherine Ellingham; the borrower record says Kathryn Ellingham.",
        "affected_fields": ["borrower_1_full_name"],
        "expected_outcome": "escalation",
        "expected_reason": "conflict",
    },
    {
        "application_number": "APP-2026-0005",
        "category": "internally_inconsistent",
        "description": "Payslip year-to-date gross figures do not form a sequence; slip 2 is out of step with slips 1 and 3.",
        "affected_fields": ["borrower_1_payg_income"],
        "expected_outcome": "escalation",
        "expected_reason": "conflict",
    },
    {
        "application_number": "APP-2026-0006",
        "category": "conflict",
        "description": "lvr_stated in the database is 0.78; total_loan_amount divided by valuation_amount is 0.85.",
        "affected_fields": ["computed_lvr"],
        "expected_outcome": "escalation",
        "expected_reason": "conflict",
    },
    {
        "application_number": "APP-2026-0007",
        "category": "unreadable",
        "description": "The credit report for Beatrix Lindqvist is written as a corrupt, unparseable PDF.",
        "affected_fields": ["borrower_1_credit_score"],
        "expected_outcome": "escalation",
        "expected_reason": "parse_failed",
    },
    {
        "application_number": "APP-2026-0008",
        "category": "conflict",
        "description": "Credit report states the subject date of birth as 30/01/1984; the borrower record says 01/03/1984.",
        "affected_fields": ["borrower_1_date_of_birth"],
        "expected_outcome": "escalation",
        "expected_reason": "conflict",
    },
]

DEFECTS_BY_APP = {d["application_number"]: d for d in DEFECTS}

# Unhelpful filenames. Ten documents is the maximum for a joint application.
FILENAME_POOL = [
    "scan_0043.pdf",
    "doc20260114_2.pdf",
    "IMG_8871.pdf",
    "attachment(3).pdf",
    "scan0002_001.pdf",
    "20260118_142233.pdf",
    "Document1.pdf",
    "IMG_0117.pdf",
    "file_copy_2.pdf",
    "scan_0044.pdf",
    "upload_final_v2.pdf",
    "DOC-000891.pdf",
]

# --- Deterministic per-borrower credit report content -----------------------
# Keyed by borrower name so the report is stable and hand-checkable.

CREDIT_PROFILES = {
    "Daniel Okonkwo": {"score": 812, "enquiries": 1, "defaults": [], "accounts": [
        ("Corella Bank", "Credit card", 8_000_00, "2019-04-11"),
        ("Northmark Auto Finance", "Personal loan", 32_000_00, "2022-09-02"),
    ]},
    "Priyanka Raghunathan": {"score": 874, "enquiries": 2, "defaults": [], "accounts": [
        ("Corella Bank", "Credit card", 15_000_00, "2016-02-20"),
        ("Vantage Mutual", "Home loan", 410_000_00, "2018-07-14"),
    ]},
    "Thomas Raghunathan": {"score": 796, "enquiries": 2, "defaults": [], "accounts": [
        ("Silverpine Credit Union", "Credit card", 6_500_00, "2015-11-30"),
    ]},
    "Gemma Halloway": {"score": 705, "enquiries": 3, "defaults": [], "accounts": [
        ("Corella Bank", "Credit card", 12_000_00, "2020-06-08"),
        ("Redgum Finance", "Personal loan", 18_500_00, "2023-03-19"),
    ]},
    "Nathan Halloway": {"score": 688, "enquiries": 4, "defaults": [
        ("2023-08-14", "Telco service default", 640_00, "Paid"),
    ], "accounts": [
        ("Silverpine Credit Union", "Credit card", 9_000_00, "2019-01-23"),
    ]},
    "Kathryn Ellingham": {"score": 758, "enquiries": 2, "defaults": [], "accounts": [
        ("Vantage Mutual", "Credit card", 7_500_00, "2021-05-17"),
    ]},
    "Hugo Vandermeer": {"score": 833, "enquiries": 1, "defaults": [], "accounts": [
        ("Corella Bank", "Credit card", 25_000_00, "2012-10-04"),
        ("Vantage Mutual", "Home loan", 640_000_00, "2017-01-25"),
        ("Northmark Auto Finance", "Car lease", 74_000_00, "2024-02-13"),
    ]},
    "Aroha Whitcombe": {"score": 741, "enquiries": 2, "defaults": [], "accounts": [
        ("Silverpine Credit Union", "Credit card", 5_000_00, "2021-09-09"),
    ]},
    "Julian Whitcombe": {"score": 723, "enquiries": 3, "defaults": [], "accounts": [
        ("Redgum Finance", "Personal loan", 22_000_00, "2022-11-28"),
    ]},
    "Beatrix Lindqvist": {"score": 861, "enquiries": 1, "defaults": [], "accounts": [
        ("Corella Bank", "Credit card", 30_000_00, "2011-03-16"),
        ("Vantage Mutual", "Home loan", 880_000_00, "2016-08-30"),
    ]},
    "Marcus Lindqvist": {"score": 802, "enquiries": 2, "defaults": [], "accounts": [
        ("Corella Bank", "Credit card", 18_000_00, "2014-12-05"),
    ]},
    "Simone Achterberg": {"score": 769, "enquiries": 2, "defaults": [], "accounts": [
        ("Redgum Finance", "Credit card", 4_500_00, "2020-02-27"),
    ]},
}

KYC_OFFICERS = [
    "L. Ashgrove",
    "M. Petrakis",
    "R. Chidozie",
    "T. Blakeney",
]


def score_band(score: int) -> str:
    """Bureau-style band label for a synthetic score on a 300-1200 scale."""
    if score >= 853:
        return "Excellent"
    if score >= 735:
        return "Very good"
    if score >= 661:
        return "Good"
    if score >= 506:
        return "Average"
    return "Below average"


def money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def au_date(value: date | str) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return value.strftime("%d/%m/%Y")


def stable_reference(prefix: str, *parts: str) -> str:
    """A fake reference number that is stable across regenerations."""
    digest = hashlib.md5("|".join(parts).encode()).hexdigest()
    return f"{prefix}-{digest[:8].upper()}"


# --- PDF plumbing -----------------------------------------------------------

STYLES = getSampleStyleSheet()
TITLE = ParagraphStyle("DocTitle", parent=STYLES["Heading1"], fontSize=16, spaceAfter=2 * mm)
HEADING = ParagraphStyle("DocHeading", parent=STYLES["Heading2"], fontSize=11, spaceBefore=5 * mm, spaceAfter=2 * mm)
BODY = ParagraphStyle("DocBody", parent=STYLES["BodyText"], fontSize=9, leading=12)
FOOTNOTE = ParagraphStyle("DocFootnote", parent=STYLES["BodyText"], fontSize=7, leading=9, textColor=colors.grey)
BANNER = ParagraphStyle("DocBanner", parent=STYLES["BodyText"], fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor("#B00020"))

TABLE_STYLE = TableStyle([
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])

KV_STYLE = TableStyle([
    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])


def _watermark(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 40)
    canvas.setFillColor(colors.HexColor("#EBEBEB"))
    canvas.translate(A4[0] / 2, A4[1] / 2)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, WATERMARK_TEXT)
    canvas.restoreState()

    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawString(20 * mm, 12 * mm, WATERMARK_TEXT)
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def write_pdf(path: Path, flowables: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Synthetic sample document",
        author="Credit Memo Agent synthetic data generator",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="watermarked", frames=[frame], onPage=_watermark)])
    doc.build(list(flowables))


def kv_table(rows: list[tuple[str, str]], widths=(48 * mm, 112 * mm)) -> Table:
    table = Table([[k, v] for k, v in rows], colWidths=list(widths))
    table.setStyle(KV_STYLE)
    return table


def grid_table(header: list[str], rows: list[list[str]], widths) -> Table:
    table = Table([header] + rows, colWidths=list(widths), repeatRows=1)
    table.setStyle(TABLE_STYLE)
    return table


# --- Document builders ------------------------------------------------------


def build_credit_report(app: dict, borrower: dict) -> list:
    profile = CREDIT_PROFILES[borrower["full_name"]]
    defect = DEFECTS_BY_APP[app["application_number"]]

    # Seeded defect 8: the report subject DOB disagrees with the borrower record.
    subject_dob = borrower["date_of_birth"]
    if defect["application_number"] == "APP-2026-0008" and borrower["is_primary"]:
        subject_dob = "1984-01-30"

    report_date = date.fromisoformat(app["lodgement_date"]) - timedelta(days=3)
    reference = stable_reference("CRB", app["application_number"], borrower["full_name"], "credit")

    story: list = [
        Paragraph("Consumer Credit Report", TITLE),
        Paragraph(
            "Synthetic bureau-style report produced for system testing. "
            "Not issued by any credit reporting body.",
            BANNER,
        ),
        Spacer(1, 4 * mm),
        Paragraph("Subject details", HEADING),
        kv_table([
            ("Subject name", borrower["full_name"]),
            ("Date of birth", au_date(subject_dob)),
            ("Residential address", borrower["residential_address"]),
            ("Report date", au_date(report_date)),
            ("Report reference", reference),
            ("Enquiring party", "Sample Lender (synthetic)"),
        ]),
        Paragraph("Credit score", HEADING),
        kv_table([
            ("Credit score", str(profile["score"])),
            ("Score range", "300 - 1200"),
            ("Score band", score_band(profile["score"])),
            ("Enquiries in last 6 months", str(profile["enquiries"])),
        ]),
        Paragraph("Listed defaults", HEADING),
    ]

    if profile["defaults"]:
        story.append(grid_table(
            ["Date listed", "Type", "Amount", "Status"],
            [[au_date(d0), kind, money(amount), status] for d0, kind, amount, status in profile["defaults"]],
            (30 * mm, 60 * mm, 35 * mm, 35 * mm),
        ))
    else:
        story.append(Paragraph("No defaults listed against this subject.", BODY))

    story += [
        Paragraph("Current credit accounts", HEADING),
        grid_table(
            ["Provider", "Account type", "Credit limit", "Date opened"],
            [[provider, kind, money(limit), au_date(opened)] for provider, kind, limit, opened in profile["accounts"]],
            (55 * mm, 40 * mm, 35 * mm, 30 * mm),
        ),
        Spacer(1, 8 * mm),
        Paragraph(
            "Disclaimer: this document is synthetic sample data generated for software testing. "
            "The subject, the providers, the reference number and every figure shown are fictional. "
            "It is not a credit report, is not issued by a credit reporting body, and must not be "
            "relied upon for any purpose.",
            FOOTNOTE,
        ),
    ]
    return story


def build_kyc(app: dict, borrower: dict) -> list:
    defect = DEFECTS_BY_APP[app["application_number"]]

    # Seeded defect 4: the KYC subject name disagrees with the borrower record.
    subject_name = borrower["full_name"]
    if defect["application_number"] == "APP-2026-0004" and borrower["is_primary"]:
        subject_name = "Katherine Ellingham"

    verification_date = date.fromisoformat(app["lodgement_date"]) - timedelta(days=5)
    officer = KYC_OFFICERS[len(borrower["full_name"]) % len(KYC_OFFICERS)]
    reference = stable_reference("KYC", app["application_number"], borrower["full_name"])

    return [
        Paragraph("Identity Verification Record", TITLE),
        Paragraph(
            "Synthetic identity verification record produced for system testing. "
            "Identification numbers are placeholders, not real documents.",
            BANNER,
        ),
        Spacer(1, 4 * mm),
        Paragraph("Verified subject", HEADING),
        kv_table([
            ("Full name", subject_name),
            ("Date of birth", au_date(borrower["date_of_birth"])),
            ("Residential address", borrower["residential_address"]),
        ]),
        Paragraph("Verification", HEADING),
        kv_table([
            ("Verification method", "Driver licence and passport (document based)"),
            ("Verification date", au_date(verification_date)),
            ("Verifying officer", officer),
            ("Verification reference", reference),
            ("Outcome", "Identity verified"),
        ]),
        Paragraph("Identification documents sighted", HEADING),
        grid_table(
            ["Document type", "Identifier", "Issuer", "Expiry"],
            [
                ["Driver licence", "PLACEHOLDER-DL-000000", "Sample state authority", au_date(date(2030, 6, 30))],
                ["Passport", "PLACEHOLDER-PP-000000", "Sample national authority", au_date(date(2031, 9, 30))],
            ],
            (38 * mm, 48 * mm, 46 * mm, 28 * mm),
        ),
        Spacer(1, 8 * mm),
        Paragraph(
            "Disclaimer: this document is synthetic sample data generated for software testing. "
            "The subject, the officer, the reference and the identification numbers shown are "
            "fictional placeholders. No real identity document exists behind this record.",
            FOOTNOTE,
        ),
    ]


def payslip_periods(app: dict) -> list[tuple[date, date, date]]:
    """Three consecutive fortnightly periods ending shortly before lodgement."""
    lodgement = date.fromisoformat(app["lodgement_date"])
    latest_end = lodgement - timedelta(days=7)
    periods = []
    for offset in (2, 1, 0):
        end = latest_end - timedelta(days=14 * offset)
        start = end - timedelta(days=13)
        periods.append((start, end, end + timedelta(days=2)))
    return periods


def ytd_sequence(app: dict, borrower: dict, gross: int) -> list[int]:
    """Year-to-date gross for each of the three slips.

    Normally consecutive multiples of the fortnightly gross. For seeded defect 5
    the middle slip is out of step, so the differences between consecutive slips
    do not equal the period gross and the figures cannot be reconciled.
    """
    base = [gross * 13, gross * 14, gross * 15]
    if app["application_number"] == "APP-2026-0005" and borrower["is_primary"]:
        base[1] = gross * 14 + 1_840_00
    return base


def build_payslip(app: dict, borrower: dict, slip_index: int) -> list:
    gross = borrower["fortnightly_gross"]
    tax = round(gross * 0.28)
    net = gross - tax
    superannuation = round(gross * 0.115)

    start, end, paid = payslip_periods(app)[slip_index]
    ytd_gross = ytd_sequence(app, borrower, gross)[slip_index]
    ytd_tax = round(ytd_gross * 0.28)

    base_hours = 76
    base_rate_cents = round(gross * 0.92 / base_hours)
    base_amount = base_rate_cents * base_hours
    allowance = gross - base_amount

    return [
        Paragraph("Payslip", TITLE),
        Paragraph(
            "Synthetic payslip produced for system testing. Employer, employee and "
            "all figures are fictional.",
            BANNER,
        ),
        Spacer(1, 4 * mm),
        Paragraph("Employer", HEADING),
        kv_table([
            ("Employer name", borrower["employer"]),
            ("ABN", borrower["employer_abn"]),
        ]),
        Paragraph("Employee", HEADING),
        kv_table([
            ("Employee name", borrower["full_name"]),
            ("Pay period start", au_date(start)),
            ("Pay period end", au_date(end)),
            ("Payment date", au_date(paid)),
            ("Pay frequency", "Fortnightly"),
        ]),
        Paragraph("Earnings", HEADING),
        grid_table(
            ["Description", "Hours", "Rate", "Amount"],
            [
                ["Ordinary hours", f"{base_hours}.00", money(base_rate_cents), money(base_amount)],
                ["Site allowance", "-", "-", money(allowance)],
            ],
            (60 * mm, 25 * mm, 35 * mm, 40 * mm),
        ),
        Paragraph("This pay period", HEADING),
        kv_table([
            ("Gross for period", money(gross)),
            ("Tax withheld", money(tax)),
            ("Net for period", money(net)),
            ("Superannuation", money(superannuation)),
        ]),
        Paragraph("Year to date", HEADING),
        kv_table([
            ("Year-to-date gross", money(ytd_gross)),
            ("Year-to-date tax", money(ytd_tax)),
        ]),
        Spacer(1, 8 * mm),
        Paragraph(
            "Disclaimer: this document is synthetic sample data generated for software "
            "testing. The employer, the employee and every figure shown are fictional.",
            FOOTNOTE,
        ),
    ]


CORRUPT_BYTES = (
    b"%PDF-1.7\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"\x00\x8f\x1a\xd3TRUNCATED SCAN OUTPUT -- SYNTHETIC DATA\x00\xff\xfe"
    b"\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Cou"
)


# --- Assembly ---------------------------------------------------------------


def planned_documents(app: dict) -> list[dict]:
    """The documents that should exist for an application, before filenames.

    `doc_id` is a stable identity used both for the filename mapping and for the
    manifest. Seeded defect 3 removes documents here; seeded defect 7 marks one
    document to be written as corrupt bytes rather than a PDF.
    """
    number = app["application_number"]
    documents: list[dict] = []

    for position, borrower in enumerate(app["borrowers"], start=1):
        name = borrower["full_name"]

        corrupt = number == "APP-2026-0007" and borrower["is_primary"]
        documents.append({
            "doc_id": f"credit_report_b{position}",
            "document_type": "equifax",
            "borrower": name,
            "borrower_position": position,
            "corrupt": corrupt,
            "builder": lambda a=app, b=borrower: build_credit_report(a, b),
        })

        documents.append({
            "doc_id": f"kyc_b{position}",
            "document_type": "kyc",
            "borrower": name,
            "borrower_position": position,
            "corrupt": False,
            "builder": lambda a=app, b=borrower: build_kyc(a, b),
        })

        # Seeded defect 3: the second applicant has no payslips at all.
        if number == "APP-2026-0003" and not borrower["is_primary"]:
            continue

        for slip in range(3):
            documents.append({
                "doc_id": f"payslip_b{position}_{slip + 1}",
                "document_type": "payslip",
                "borrower": name,
                "borrower_position": position,
                "corrupt": False,
                "builder": lambda a=app, b=borrower, s=slip: build_payslip(a, b, s),
            })

    return documents


def assign_filenames(app_number: str, documents: list[dict]) -> None:
    """Attach an unhelpful filename to each document, decorrelated from its type.

    Ordering by a digest of (application, doc_id) means the filename is stable
    across regenerations but carries no signal about what the document contains.
    """
    if len(documents) > len(FILENAME_POOL):
        raise ValueError(
            f"{app_number}: {len(documents)} documents but only "
            f"{len(FILENAME_POOL)} filenames in the pool"
        )

    order = sorted(
        documents,
        key=lambda d: hashlib.md5(f"{app_number}|{d['doc_id']}".encode()).hexdigest(),
    )
    for filename, document in zip(FILENAME_POOL, order):
        document["filename"] = filename


def build() -> None:
    if DOCUMENTS_DIR.exists():
        shutil.rmtree(DOCUMENTS_DIR)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, list[dict]] = {}
    total = 0

    for app in APPLICATIONS:
        number = app["application_number"]
        documents = planned_documents(app)
        assign_filenames(number, documents)

        folder = DOCUMENTS_DIR / number
        folder.mkdir(parents=True, exist_ok=True)

        entries = []
        for document in documents:
            path = folder / document["filename"]
            if document["corrupt"]:
                path.write_bytes(CORRUPT_BYTES)
            else:
                write_pdf(path, document["builder"]())

            entries.append({
                "filename": document["filename"],
                "document_type": document["document_type"],
                "borrower": document["borrower"],
                "borrower_position": document["borrower_position"],
                "readable": not document["corrupt"],
            })
            total += 1

        manifest[number] = sorted(entries, key=lambda e: e["filename"])

    payload = {
        "note": (
            "Ground truth for the synthetic data set. Tests read expectations from "
            "here rather than hard-coding them. `manifest` records what each "
            "unhelpfully named file actually is, for test assertions only - the "
            "agent must classify from content."
        ),
        "defects": DEFECTS,
        "manifest": manifest,
    }
    DEFECTS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {total} documents across {len(APPLICATIONS)} applications to {DOCUMENTS_DIR}")
    print(f"Wrote ground truth to {DEFECTS_PATH}")


if __name__ == "__main__":
    build()
