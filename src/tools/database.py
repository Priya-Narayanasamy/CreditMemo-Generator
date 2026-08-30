"""Read-only access to the application database.

Every function returns typed Pydantic models, and every model can build the
`Provenance` for any field it carries - table, column and row key. A value that
reaches the ledger from here is always attributable to a specific cell.

The connection is opened read-only. Nothing in this system writes to the deals
database.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel

from config import DB_PATH
from src.state import Provenance


class RowModel(BaseModel):
    """A database row that knows where it came from."""

    _table: str = ""
    _row_key_field: str = ""

    def provenance_for(self, column: str) -> Provenance:
        if column not in type(self).model_fields:
            raise KeyError(f"{type(self).__name__} has no column {column!r}")
        return Provenance(
            source_kind="database",
            detail={
                "table": self._table,
                "column": column,
                "row_key": str(getattr(self, self._row_key_field)),
            },
        )


class Application(RowModel):
    _table = "applications"
    _row_key_field = "application_number"

    application_number: str
    loan_purpose: Literal["owner_occupied", "investment"]
    applicant_structure: Literal["single", "joint"]
    valuation_amount: int              # cents
    valuation_date: date
    total_loan_amount: int             # cents
    lvr_stated: float                  # REAL in the system of record, by design
    security_address: str
    lodgement_date: date


class Borrower(RowModel):
    _table = "borrowers"
    _row_key_field = "borrower_id"

    borrower_id: int
    application_number: str
    full_name: str
    date_of_birth: date
    residential_address: str
    phone: str
    email: str
    is_primary: bool


class RelatedParty(RowModel):
    _table = "related_parties"
    _row_key_field = "party_id"

    party_id: int
    application_number: str
    party_type: Literal["accountant", "solicitor", "broker"]
    name: str
    email: str
    phone: str
    firm_name: str


class LoanSplit(RowModel):
    _table = "loan_splits"
    _row_key_field = "split_id"

    split_id: int
    application_number: str
    split_number: int
    amount: int                        # cents
    product_type: Literal["variable", "fixed_3yr", "interest_only"]
    term_months: int


class ApplicationFile(BaseModel):
    """Everything the database holds about one application."""

    application: Application
    borrowers: list[Borrower]
    related_parties: list[RelatedParty]
    loan_splits: list[LoanSplit]

    @property
    def primary_borrower(self) -> Borrower:
        return next(b for b in self.borrowers if b.is_primary)

    def borrower_at(self, position: int) -> Borrower:
        """Borrower 1 is the primary; borrower 2 is the other, if there is one."""
        return self.ordered_borrowers()[position - 1]

    def ordered_borrowers(self) -> list[Borrower]:
        return sorted(self.borrowers, key=lambda b: (not b.is_primary, b.borrower_id))


class DatabaseUnavailable(RuntimeError):
    """Raised only when the database file itself is absent or unreadable.

    A missing application is not this - that returns None, so the agent can
    escalate rather than crash.
    """


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise DatabaseUnavailable(
            f"{path} does not exist. Run `python data/generate_db.py` to build it."
        )

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def list_application_numbers(db_path: Path | None = None) -> list[str]:
    with _connect(db_path) as conn:
        return [
            row["application_number"]
            for row in conn.execute(
                "SELECT application_number FROM applications ORDER BY application_number"
            )
        ]


def get_application(application_number: str, db_path: Path | None = None) -> Application | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM applications WHERE application_number = ?", (application_number,)
        ).fetchone()

    return Application.model_validate(dict(row)) if row else None


def get_borrowers(application_number: str, db_path: Path | None = None) -> list[Borrower]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM borrowers WHERE application_number = ? "
            "ORDER BY is_primary DESC, borrower_id",
            (application_number,),
        ).fetchall()

    return [Borrower.model_validate(dict(row)) for row in rows]


def get_related_parties(application_number: str, db_path: Path | None = None) -> list[RelatedParty]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM related_parties WHERE application_number = ? ORDER BY party_id",
            (application_number,),
        ).fetchall()

    return [RelatedParty.model_validate(dict(row)) for row in rows]


def get_loan_splits(application_number: str, db_path: Path | None = None) -> list[LoanSplit]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM loan_splits WHERE application_number = ? ORDER BY split_number",
            (application_number,),
        ).fetchall()

    return [LoanSplit.model_validate(dict(row)) for row in rows]


def get_application_file(
    application_number: str, db_path: Path | None = None
) -> ApplicationFile | None:
    """The whole record for one application, or None if there is no such application."""
    application = get_application(application_number, db_path)
    if application is None:
        return None

    return ApplicationFile(
        application=application,
        borrowers=get_borrowers(application_number, db_path),
        related_parties=get_related_parties(application_number, db_path),
        loan_splits=get_loan_splits(application_number, db_path),
    )
