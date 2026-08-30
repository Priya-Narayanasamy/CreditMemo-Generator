"""Phase 1 tests: the synthetic data set itself.

These assert the shape and the ground truth of the generated data - that the
database is internally consistent, that every document is text-extractable with
real table structure, and that each seeded defect is present and isolated to its
own application. Expectations are read from `data/defects.json`, never hard-coded.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pdfplumber
import pytest

from config import DB_PATH, DEFECTS_PATH, DOCUMENTS_DIR, WATERMARK_TEXT

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DEFECTS_PATH.exists(),
    reason="run data/generate_db.py and data/generate_docs.py first",
)


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    return json.loads(DEFECTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def conn():
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def doc_text() -> dict[tuple[str, str], str]:
    """Extracted text for every readable document, keyed by (application, filename)."""
    cache: dict[tuple[str, str], str] = {}
    for folder in sorted(DOCUMENTS_DIR.iterdir()):
        for path in sorted(folder.iterdir()):
            try:
                with pdfplumber.open(path) as pdf:
                    cache[(folder.name, path.name)] = "\n".join(
                        page.extract_text() or "" for page in pdf.pages
                    )
            except Exception:
                continue
    return cache


def au(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{day}/{month}/{year}"


# --- Database ---------------------------------------------------------------


def test_eight_applications_with_the_required_mix(conn):
    rows = conn.execute(
        "SELECT loan_purpose, applicant_structure, COUNT(*) AS n FROM applications "
        "GROUP BY loan_purpose, applicant_structure"
    ).fetchall()
    mix = {(r["loan_purpose"], r["applicant_structure"]): r["n"] for r in rows}

    assert mix == {
        ("owner_occupied", "single"): 2,
        ("owner_occupied", "joint"): 2,
        ("investment", "single"): 2,
        ("investment", "joint"): 2,
    }


def test_money_columns_are_integer_cents(conn):
    for row in conn.execute(
        "SELECT application_number, valuation_amount, total_loan_amount FROM applications"
    ):
        assert isinstance(row["valuation_amount"], int), row["application_number"]
        assert isinstance(row["total_loan_amount"], int), row["application_number"]

    for row in conn.execute("SELECT split_id, amount FROM loan_splits"):
        assert isinstance(row["amount"], int), row["split_id"]


def test_splits_sum_to_the_total_loan_amount(conn):
    for row in conn.execute(
        "SELECT a.application_number, a.total_loan_amount, SUM(s.amount) AS split_total, "
        "COUNT(s.split_id) AS split_count "
        "FROM applications a JOIN loan_splits s USING (application_number) "
        "GROUP BY a.application_number"
    ):
        assert row["split_total"] == row["total_loan_amount"], row["application_number"]
        assert 1 <= row["split_count"] <= 3, row["application_number"]


def test_loan_amounts_sit_in_the_specified_range(conn):
    for row in conn.execute("SELECT application_number, total_loan_amount FROM applications"):
        assert 400_000_00 <= row["total_loan_amount"] <= 1_800_000_00, row["application_number"]


def test_borrower_count_matches_applicant_structure(conn):
    for row in conn.execute(
        "SELECT a.application_number, a.applicant_structure, COUNT(b.borrower_id) AS n, "
        "SUM(b.is_primary) AS primaries "
        "FROM applications a JOIN borrowers b USING (application_number) "
        "GROUP BY a.application_number"
    ):
        expected = 1 if row["applicant_structure"] == "single" else 2
        assert row["n"] == expected, row["application_number"]
        assert row["primaries"] == 1, row["application_number"]


def test_only_the_seeded_application_has_a_stated_lvr_mismatch(conn, ground_truth):
    defect = next(d for d in ground_truth["defects"] if d["category"] == "conflict"
                  and "lvr_stated" in d["description"])

    mismatched = [
        row["application_number"]
        for row in conn.execute(
            "SELECT application_number, total_loan_amount, valuation_amount, lvr_stated "
            "FROM applications"
        )
        if abs(row["total_loan_amount"] / row["valuation_amount"] - row["lvr_stated"]) > 0.0001
    ]

    assert mismatched == [defect["application_number"]]


# --- Documents --------------------------------------------------------------


def test_every_application_has_a_document_folder(conn, ground_truth):
    applications = {r["application_number"] for r in conn.execute(
        "SELECT application_number FROM applications")}

    assert set(ground_truth["manifest"]) == applications
    for number in applications:
        assert (DOCUMENTS_DIR / number).is_dir()


def test_filenames_are_unhelpful(ground_truth):
    """No filename may leak the document type or the subject."""
    leaks = ("equifax", "credit", "kyc", "payslip", "pay", "identity", "report")

    for entries in ground_truth["manifest"].values():
        for entry in entries:
            stem = Path(entry["filename"]).stem.lower()
            assert not any(leak in stem for leak in leaks), entry["filename"]
            assert entry["borrower"].split()[-1].lower() not in stem, entry["filename"]


def test_manifest_matches_what_is_on_disk(ground_truth):
    for number, entries in ground_truth["manifest"].items():
        on_disk = {path.name for path in (DOCUMENTS_DIR / number).iterdir()}
        assert on_disk == {entry["filename"] for entry in entries}


def test_readable_documents_extract_text_and_tables(ground_truth):
    for number, entries in ground_truth["manifest"].items():
        for entry in entries:
            if not entry["readable"]:
                continue
            path = DOCUMENTS_DIR / number / entry["filename"]
            with pdfplumber.open(path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
                tables = sum(len(page.extract_tables()) for page in pdf.pages)

            assert len(text) > 400, f"{number}/{entry['filename']} extracted almost no text"
            assert tables >= 2, f"{number}/{entry['filename']} has no table structure"


def test_every_readable_document_is_watermarked(doc_text):
    for (number, filename), text in doc_text.items():
        assert WATERMARK_TEXT in text, f"{number}/{filename} is not watermarked"


def test_exactly_one_document_is_unparseable(ground_truth, doc_text):
    declared_unreadable = {
        (number, entry["filename"])
        for number, entries in ground_truth["manifest"].items()
        for entry in entries
        if not entry["readable"]
    }
    all_documents = {
        (number, entry["filename"])
        for number, entries in ground_truth["manifest"].items()
        for entry in entries
    }

    actually_unparseable = all_documents - set(doc_text)
    assert actually_unparseable == declared_unreadable


def test_document_set_per_borrower(ground_truth):
    """One credit report and one KYC record per borrower; three payslips unless
    the application's seeded defect removes them."""
    missing_defect = next(d for d in ground_truth["defects"] if d["category"] == "missing")

    for number, entries in ground_truth["manifest"].items():
        counts = Counter((entry["borrower"], entry["document_type"]) for entry in entries)
        borrowers = {entry["borrower"] for entry in entries}

        for borrower in borrowers:
            assert counts[(borrower, "equifax")] == 1, (number, borrower)
            assert counts[(borrower, "kyc")] == 1, (number, borrower)

            slips = counts[(borrower, "payslip")]
            if number == missing_defect["application_number"] and slips == 0:
                continue
            assert slips == 3, (number, borrower, slips)


# --- Seeded defects ---------------------------------------------------------


def test_defects_cover_every_application(conn, ground_truth):
    applications = {r["application_number"] for r in conn.execute(
        "SELECT application_number FROM applications")}
    declared = [d["application_number"] for d in ground_truth["defects"]]

    assert sorted(declared) == sorted(applications)
    assert len(declared) == len(set(declared)), "one defect entry per application"


def test_two_applications_are_clean(ground_truth):
    clean = [d for d in ground_truth["defects"] if d["category"] == "clean"]

    assert len(clean) == 2
    assert all(d["expected_outcome"] == "approval" for d in clean)
    assert all(d["affected_fields"] == [] for d in clean)


def test_every_defective_application_declares_an_escalation(ground_truth):
    for defect in ground_truth["defects"]:
        if defect["category"] == "clean":
            continue
        assert defect["expected_outcome"] == "escalation", defect["application_number"]
        assert defect["expected_reason"] in {"not_found", "parse_failed", "conflict", "low_confidence"}
        assert defect["affected_fields"], defect["application_number"]


def test_identity_details_match_the_record_except_where_seeded(conn, ground_truth, doc_text):
    """The name and DOB in every credit report and KYC record agree with the
    borrower row, except in the two applications where a conflict is seeded."""
    seeded = {
        d["application_number"]
        for d in ground_truth["defects"]
        if d["category"] == "conflict" and d["affected_fields"] != ["computed_lvr"]
    }
    found: set[str] = set()

    for number, entries in ground_truth["manifest"].items():
        borrowers = {
            row["full_name"]: row
            for row in conn.execute(
                "SELECT * FROM borrowers WHERE application_number = ?", (number,)
            )
        }
        for entry in entries:
            if not entry["readable"] or entry["document_type"] not in {"equifax", "kyc"}:
                continue
            text = doc_text[(number, entry["filename"])]
            borrower = borrowers[entry["borrower"]]

            if borrower["full_name"] not in text or au(borrower["date_of_birth"]) not in text:
                found.add(number)

    assert found == seeded


def test_seeded_ytd_figures_do_not_form_a_sequence(ground_truth, doc_text):
    defect = next(d for d in ground_truth["defects"]
                  if d["category"] == "internally_inconsistent")
    number = defect["application_number"]

    figures = []
    for entry in ground_truth["manifest"][number]:
        if entry["document_type"] != "payslip":
            continue
        text = doc_text[(number, entry["filename"])]
        period_gross = _cents(text, "Gross for period")
        ytd = _cents(text, "Year-to-date gross")
        figures.append((_period_end(text), period_gross, ytd))

    figures.sort()
    gaps = [later[2] - earlier[2] for earlier, later in zip(figures, figures[1:])]
    period_gross = {f[1] for f in figures}

    assert len(period_gross) == 1, "period gross should be constant across the slips"
    assert len(gaps) == 2
    assert any(gap != figures[0][1] for gap in gaps), (
        "the seeded year-to-date figures should not reconcile against period gross"
    )


def _cents(text: str, label: str) -> int:
    line = next(line for line in text.splitlines() if line.startswith(label))
    amount = line.rsplit("$", 1)[1].replace(",", "")
    dollars, cents = amount.split(".")
    return int(dollars) * 100 + int(cents)


def _period_end(text: str) -> str:
    line = next(line for line in text.splitlines() if line.startswith("Pay period end"))
    day, month, year = line.split()[-1].split("/")
    return f"{year}-{month}-{day}"


def test_regeneration_is_deterministic(ground_truth):
    """Rebuilding the ground truth from the generators must not change it."""
    import importlib

    generate_docs = importlib.import_module("generate_docs")

    for app in importlib.import_module("generate_db").APPLICATIONS:
        number = app["application_number"]
        documents = generate_docs.planned_documents(app)
        generate_docs.assign_filenames(number, documents)

        rebuilt = sorted(
            (
                {
                    "filename": d["filename"],
                    "document_type": d["document_type"],
                    "borrower": d["borrower"],
                    "borrower_position": d["borrower_position"],
                    "readable": not d["corrupt"],
                }
                for d in documents
            ),
            key=lambda e: e["filename"],
        )

        assert rebuilt == ground_truth["manifest"][number], number
