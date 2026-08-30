"""Phase 3 tests: database, parsing, classification and extraction.

The recurring theme is that a tool failure is a value returned into state, never
an exception escaping a node. Corrupt documents, missing files and absent
applications all come back as data the agent can act on.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from config import DB_PATH, DEFECTS_PATH, DOCUMENTS_DIR
from src.tools.database import (
    DatabaseUnavailable,
    get_application,
    get_application_file,
    get_borrowers,
    get_loan_splits,
    list_application_numbers,
)
from src.tools.documents import DocumentRef, list_documents, load_and_classify
from src.tools.extraction import (
    EquifaxExtraction,
    KycExtraction,
    LocalTableExtractor,
    PayslipExtraction,
    parse_au_date,
    parse_cents,
    parse_int,
)
from src.tools.models import log_model_call, read_call_log
from src.tools.parsing import PdfplumberParser, clean_cell, parser_for_attempt

pytestmark = pytest.mark.skipif(
    not DB_PATH.exists() or not DOCUMENTS_DIR.exists(),
    reason="run the data generators first",
)


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    return json.loads(DEFECTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def extractor() -> LocalTableExtractor:
    return LocalTableExtractor()


# --- Database ---------------------------------------------------------------


def test_lists_every_application():
    assert len(list_application_numbers()) == 8
    assert list_application_numbers()[0] == "APP-2026-0001"


def test_money_comes_back_as_integer_cents():
    application = get_application("APP-2026-0001")

    assert application.total_loan_amount == 585_000_00
    assert isinstance(application.total_loan_amount, int)
    assert isinstance(application.valuation_date, date)


def test_every_column_can_build_its_own_provenance():
    application = get_application("APP-2026-0001")
    provenance = application.provenance_for("total_loan_amount")

    assert provenance.source_kind == "database"
    assert provenance.detail == {
        "table": "applications",
        "column": "total_loan_amount",
        "row_key": "APP-2026-0001",
    }


def test_provenance_for_an_unknown_column_raises():
    with pytest.raises(KeyError):
        get_application("APP-2026-0001").provenance_for("nonexistent")


def test_borrower_provenance_points_at_the_row():
    borrower = get_borrowers("APP-2026-0001")[0]

    assert borrower.provenance_for("full_name").detail["row_key"] == str(borrower.borrower_id)


def test_a_missing_application_returns_none_rather_than_raising():
    assert get_application("APP-9999-9999") is None
    assert get_application_file("APP-9999-9999") is None


def test_a_missing_database_raises_a_named_error(tmp_path):
    with pytest.raises(DatabaseUnavailable):
        get_application("APP-2026-0001", db_path=tmp_path / "nope.db")


def test_borrowers_are_ordered_primary_first():
    file = get_application_file("APP-2026-0002")

    assert file.ordered_borrowers()[0].is_primary
    assert file.borrower_at(1) == file.primary_borrower
    assert not file.borrower_at(2).is_primary


def test_splits_sum_to_the_loan_amount():
    file = get_application_file("APP-2026-0005")

    assert sum(s.amount for s in file.loan_splits) == file.application.total_loan_amount
    assert [s.split_number for s in get_loan_splits("APP-2026-0005")] == [1, 2, 3]


# --- Parsing ----------------------------------------------------------------


def test_a_readable_document_parses_with_pages_and_tables():
    ref = list_documents("APP-2026-0001")[0]
    parsed = PdfplumberParser().parse(Path(ref.path))

    assert parsed.ok
    assert parsed.page_count >= 1
    assert parsed.pairs()
    assert parsed.parser == "pdfplumber"


def test_a_corrupt_document_returns_a_failure_value_not_an_exception(ground_truth):
    corrupt = next(
        (number, entry["filename"])
        for number, entries in ground_truth["manifest"].items()
        for entry in entries
        if not entry["readable"]
    )
    parsed = PdfplumberParser().parse(DOCUMENTS_DIR / corrupt[0] / corrupt[1])

    assert parsed.ok is False
    assert parsed.error
    assert parsed.pages == []


def test_a_missing_file_returns_a_failure_value():
    parsed = PdfplumberParser().parse(Path("no/such/file.pdf"))

    assert parsed.ok is False
    assert "not found" in parsed.error


def test_the_watermark_is_filtered_out_of_table_cells():
    """The watermark crosses table cells. It must not reach an extracted value,
    and it must not swallow one either."""
    ref = next(r for r in list_documents("APP-2026-0003"))
    parsed = PdfplumberParser().parse(Path(ref.path))

    for key, value in parsed.pairs().items():
        assert "SAMPLE" not in value
        assert "SYNTHETIC" not in key


def test_the_watermark_still_appears_in_the_page_text():
    """Filtering is for table extraction only - the document is still visibly marked."""
    ref = list_documents("APP-2026-0001")[0]

    assert "SAMPLE" in PdfplumberParser().parse(Path(ref.path)).text


def test_a_single_character_value_survives_cleaning():
    """A one-character cell is data, not furniture. Discarding it would turn a
    real value into a phantom gap."""
    assert clean_cell("4") == "4"
    assert clean_cell("Daniel\nOkonkwo") == "Daniel Okonkwo"
    assert clean_cell(None) == ""
    assert clean_cell("  ") == ""


def test_successive_attempts_use_different_parser_settings():
    """Retrying a parse failure with identical settings would be pointless."""
    first, second = parser_for_attempt(0), parser_for_attempt(1)

    assert first.table_settings != second.table_settings


def test_page_of_locates_a_string_for_provenance():
    ref = list_documents("APP-2026-0001")[0]
    parsed = PdfplumberParser().parse(Path(ref.path))

    assert parsed.page_of("SAMPLE") == 1
    assert parsed.page_of("this string is not in the document") is None


# --- Documents and classification -------------------------------------------


def test_list_documents_finds_the_whole_folder(ground_truth):
    for number, entries in ground_truth["manifest"].items():
        assert {r.filename for r in list_documents(number)} == {e["filename"] for e in entries}


def test_an_application_with_no_folder_returns_no_documents():
    assert list_documents("APP-9999-9999") == []


def test_every_document_classifies_from_content_not_filename(ground_truth, extractor):
    """The same filename is a payslip in one application and a credit report in
    another, so a correct classification cannot have come from the name."""
    for number, entries in ground_truth["manifest"].items():
        expected = {e["filename"]: e["document_type"] for e in entries if e["readable"]}
        for ref in list_documents(number):
            if ref.filename not in expected:
                continue
            loaded = load_and_classify(ref, extractor)

            assert loaded.document_type == expected[ref.filename], f"{number}/{ref.filename}"
            assert loaded.classification.confidence > 0


def test_the_same_filename_carries_different_types_across_applications(ground_truth):
    by_filename: dict[str, set[str]] = {}
    for entries in ground_truth["manifest"].values():
        for entry in entries:
            by_filename.setdefault(entry["filename"], set()).add(entry["document_type"])

    assert any(len(types) > 1 for types in by_filename.values())


def test_an_unparseable_document_classifies_as_unknown(ground_truth, extractor):
    number, filename = next(
        (number, entry["filename"])
        for number, entries in ground_truth["manifest"].items()
        for entry in entries
        if not entry["readable"]
    )
    ref = next(r for r in list_documents(number) if r.filename == filename)
    loaded = load_and_classify(ref, extractor)

    assert loaded.document_type == "unknown"
    assert loaded.classification.confidence == 0.0
    assert loaded.classification.reasoning
    assert not loaded.usable


def test_document_provenance_names_the_file_and_the_type():
    ref = DocumentRef(filename="scan_0043.pdf", path="x", size_bytes=1)
    provenance = ref.provenance("payslip", page=2)

    assert provenance.detail == {"filename": "scan_0043.pdf", "page": 2, "document_type": "payslip"}
    assert "scan_0043.pdf" in provenance.describe()


# --- Extraction -------------------------------------------------------------


def first_of_type(number: str, document_type: str, extractor):
    for ref in list_documents(number):
        loaded = load_and_classify(ref, extractor)
        if loaded.document_type == document_type:
            return loaded
    raise AssertionError(f"no {document_type} in {number}")


def test_payslip_extraction_returns_integer_cents(extractor):
    loaded = first_of_type("APP-2026-0001", "payslip", extractor)
    result = extractor.extract(loaded.parsed, "payslip")
    payslip = PayslipExtraction.model_validate(result.data)

    assert result.ok
    assert payslip.employee_name == "Daniel Okonkwo"
    assert payslip.gross_for_period == 4_150_00
    assert isinstance(payslip.gross_for_period, int)
    assert payslip.pay_frequency == "fortnightly"
    assert payslip.period_end is not None


def test_credit_report_extraction_reads_score_enquiries_and_tables(extractor):
    loaded = first_of_type("APP-2026-0003", "equifax", extractor)
    result = extractor.extract(loaded.parsed, "equifax")
    report = EquifaxExtraction.model_validate(result.data)

    assert report.credit_score in range(300, 1201)
    assert report.enquiries_last_6_months is not None
    assert report.credit_accounts
    assert report.subject_date_of_birth is not None


def test_kyc_extraction_copies_the_name_exactly_as_written(extractor):
    """Seeded defect 4. The extractor must not silently correct Katherine to
    Kathryn - that would erase the discrepancy the analyst needs to see."""
    loaded = first_of_type("APP-2026-0004", "kyc", extractor)
    result = extractor.extract(loaded.parsed, "kyc")
    kyc = KycExtraction.model_validate(result.data)

    assert kyc.subject_name == "Katherine Ellingham"


def test_extraction_from_an_unparseable_document_returns_a_failure_value(ground_truth, extractor):
    number, filename = next(
        (number, entry["filename"])
        for number, entries in ground_truth["manifest"].items()
        for entry in entries
        if not entry["readable"]
    )
    ref = next(r for r in list_documents(number) if r.filename == filename)
    loaded = load_and_classify(ref, extractor)
    result = extractor.extract(loaded.parsed, "equifax")

    assert result.ok is False
    assert result.error
    assert result.typed() is None


def test_extracting_the_wrong_schema_returns_a_failure_not_an_exception(extractor):
    loaded = first_of_type("APP-2026-0001", "payslip", extractor)

    assert extractor.extract(loaded.parsed, "unknown").ok is False


def test_absent_fields_come_back_as_none_never_invented(extractor):
    """Most credit reports list no defaults. That must extract as an empty list,
    not as a plausible-looking default."""
    loaded = first_of_type("APP-2026-0001", "equifax", extractor)
    report = EquifaxExtraction.model_validate(extractor.extract(loaded.parsed, "equifax").data)

    assert report.listed_defaults == []


def test_every_extraction_field_is_optional():
    """Never make a field non-optional to force a model to produce it."""
    for schema in (EquifaxExtraction, KycExtraction, PayslipExtraction):
        empty = schema()
        for name, value in empty.model_dump().items():
            assert value is None or value == [], f"{schema.__name__}.{name} is not optional"


def test_extraction_is_deterministic(extractor):
    loaded = first_of_type("APP-2026-0001", "payslip", extractor)
    runs = {extractor.extract(loaded.parsed, "payslip").model_dump_json() for _ in range(5)}

    assert len(runs) == 1


# --- Value parsing helpers --------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("$4,150.00", 415000), ("$0.05", 5), ("$585,000.00", 58500000), ("4150", 415000),
     ("-$1.50", -150), ("", None), (None, None), ("not money", None)],
)
def test_parse_cents(text, expected):
    assert parse_cents(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("09/01/2026", date(2026, 1, 9)), ("2026-01-09", date(2026, 1, 9)),
     ("", None), (None, None), ("sometime", None)],
)
def test_parse_au_date(text, expected):
    assert parse_au_date(text) == expected


def test_parse_au_date_reads_day_first():
    """01/03/1984 is the first of March, not the third of January."""
    assert parse_au_date("01/03/1984") == date(1984, 3, 1)


@pytest.mark.parametrize("text, expected", [("688", 688), ("1,200", 1200), ("", None), (None, None)])
def test_parse_int(text, expected):
    assert parse_int(text) == expected


# --- Model call logging -----------------------------------------------------


def test_every_model_call_is_logged_with_prompt_response_and_tokens(tmp_path, monkeypatch):
    log = tmp_path / "model_calls.jsonl"
    monkeypatch.setattr("src.tools.models.MODEL_CALL_LOG", log)

    log_model_call(
        purpose="classify_document", model="test-model", provider="nebius",
        prompt="classify this", response={"document_type": "payslip"},
        input_tokens=120, output_tokens=8, duration_ms=42,
    )
    record = json.loads(log.read_text(encoding="utf-8").strip())

    assert record["purpose"] == "classify_document"
    assert record["prompt"] == "classify this"
    assert record["input_tokens"] == 120 and record["output_tokens"] == 8
    assert record["temperature"] == 0
    assert record["timestamp"]


def test_an_api_key_never_reaches_the_log(tmp_path, monkeypatch):
    log = tmp_path / "model_calls.jsonl"
    monkeypatch.setattr("src.tools.models.MODEL_CALL_LOG", log)
    monkeypatch.setattr("src.tools.models.NEBIUS_API_KEY", "sk-supersecretkey-000")

    log_model_call(
        purpose="extract_payslip", model="m", provider="nebius",
        prompt="key sk-supersecretkey-000 inside", response="ok",
        input_tokens=1, output_tokens=1, duration_ms=1,
    )

    assert "sk-supersecretkey-000" not in log.read_text(encoding="utf-8")
    assert "[redacted]" in log.read_text(encoding="utf-8")


def test_a_failed_call_is_still_logged(tmp_path, monkeypatch):
    log = tmp_path / "model_calls.jsonl"
    monkeypatch.setattr("src.tools.models.MODEL_CALL_LOG", log)

    from src.tools.models import logged_call

    def boom():
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        logged_call("extract_payslip", "nebius", "m", "prompt", boom)

    record = json.loads(log.read_text(encoding="utf-8").strip())

    assert "connection reset" in record["error"]


def test_reading_an_absent_log_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tools.models.MODEL_CALL_LOG", tmp_path / "absent.jsonl")

    assert read_call_log() == []
