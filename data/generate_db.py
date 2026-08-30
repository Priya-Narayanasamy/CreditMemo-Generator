"""Generate the synthetic application database.

All data is invented. Money is stored as integer cents throughout - never float.
The only float column is `lvr_stated`, which mirrors the system-of-record ratio so
the agent can cross-check its own Decimal calculation against it.

Deterministic: no randomness. Regenerating produces an identical dataset.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH  # noqa: E402

SCHEMA = """
DROP TABLE IF EXISTS loan_splits;
DROP TABLE IF EXISTS related_parties;
DROP TABLE IF EXISTS borrowers;
DROP TABLE IF EXISTS applications;

CREATE TABLE applications (
    application_number  TEXT PRIMARY KEY,
    loan_purpose        TEXT NOT NULL CHECK (loan_purpose IN ('owner_occupied', 'investment')),
    applicant_structure TEXT NOT NULL CHECK (applicant_structure IN ('single', 'joint')),
    valuation_amount    INTEGER NOT NULL,
    valuation_date      DATE NOT NULL,
    total_loan_amount   INTEGER NOT NULL,
    lvr_stated          REAL NOT NULL,
    security_address    TEXT NOT NULL,
    lodgement_date      DATE NOT NULL
);

CREATE TABLE borrowers (
    borrower_id         INTEGER PRIMARY KEY,
    application_number  TEXT NOT NULL REFERENCES applications(application_number),
    full_name           TEXT NOT NULL,
    date_of_birth       DATE NOT NULL,
    residential_address TEXT NOT NULL,
    phone               TEXT NOT NULL,
    email               TEXT NOT NULL,
    is_primary          BOOLEAN NOT NULL
);

CREATE TABLE related_parties (
    party_id            INTEGER PRIMARY KEY,
    application_number  TEXT NOT NULL REFERENCES applications(application_number),
    party_type          TEXT NOT NULL CHECK (party_type IN ('accountant', 'solicitor', 'broker')),
    name                TEXT NOT NULL,
    email               TEXT NOT NULL,
    phone               TEXT NOT NULL,
    firm_name           TEXT NOT NULL
);

CREATE TABLE loan_splits (
    split_id            INTEGER PRIMARY KEY,
    application_number  TEXT NOT NULL REFERENCES applications(application_number),
    split_number        INTEGER NOT NULL,
    amount              INTEGER NOT NULL,
    product_type        TEXT NOT NULL CHECK (product_type IN ('variable', 'fixed_3yr', 'interest_only')),
    term_months         INTEGER NOT NULL
);
"""


def d(dollars: int) -> int:
    """Whole dollars to integer cents."""
    return dollars * 100


# Each application's splits sum exactly to total_loan_amount.
# `lvr_stated` equals loan / valuation, except APP-2026-0006 (seeded defect 6).
# The `employer`, `employer_abn` and `fortnightly_gross` keys on each borrower are
# not database columns - generate_docs.py reads them to build the payslips.
APPLICATIONS = [
    {
        "application_number": "APP-2026-0001",
        "loan_purpose": "owner_occupied",
        "applicant_structure": "single",
        "valuation_amount": d(780_000),
        "valuation_date": "2026-01-08",
        "total_loan_amount": d(585_000),
        "lvr_stated": 0.75,
        "security_address": "14 Waratah Grove, Ashfield NSW 2131",
        "lodgement_date": "2026-01-14",
        "borrowers": [
            {
                "full_name": "Daniel Okonkwo",
                "date_of_birth": "1985-03-22",
                "residential_address": "14 Waratah Grove, Ashfield NSW 2131",
                "phone": "0400 111 222",
                "email": "daniel.okonkwo@examplemail.test",
                "is_primary": True,
                "employer": "Harbourline Logistics Pty Ltd",
                "employer_abn": "11 222 333 444",
                "fortnightly_gross": d(4_150),
            }
        ],
        "related_parties": [
            {
                "party_type": "broker",
                "name": "Melissa Tran",
                "email": "m.tran@brightpathfinance.test",
                "phone": "0411 555 010",
                "firm_name": "Brightpath Finance Group",
            },
            {
                "party_type": "solicitor",
                "name": "Owen Fitzgerald",
                "email": "o.fitzgerald@quaylegal.test",
                "phone": "02 8000 1010",
                "firm_name": "Quay Legal Partners",
            },
        ],
        "splits": [
            {"amount": d(585_000), "product_type": "variable", "term_months": 360},
        ],
    },
    {
        "application_number": "APP-2026-0002",
        "loan_purpose": "owner_occupied",
        "applicant_structure": "joint",
        "valuation_amount": d(1_240_000),
        "valuation_date": "2026-01-12",
        "total_loan_amount": d(930_000),
        "lvr_stated": 0.75,
        "security_address": "7 Kurrajong Circuit, Kew VIC 3101",
        "lodgement_date": "2026-01-19",
        "borrowers": [
            {
                "full_name": "Priyanka Raghunathan",
                "date_of_birth": "1982-11-04",
                "residential_address": "7 Kurrajong Circuit, Kew VIC 3101",
                "phone": "0402 333 444",
                "email": "p.raghunathan@examplemail.test",
                "is_primary": True,
                "employer": "Meridian Health Services Ltd",
                "employer_abn": "22 333 444 555",
                "fortnightly_gross": d(5_600),
            },
            {
                "full_name": "Thomas Raghunathan",
                "date_of_birth": "1980-06-17",
                "residential_address": "7 Kurrajong Circuit, Kew VIC 3101",
                "phone": "0402 333 445",
                "email": "t.raghunathan@examplemail.test",
                "is_primary": False,
                "employer": "Coldstream Engineering Pty Ltd",
                "employer_abn": "33 444 555 666",
                "fortnightly_gross": d(4_800),
            },
        ],
        "related_parties": [
            {
                "party_type": "broker",
                "name": "Callum Whitfield",
                "email": "c.whitfield@northreachloans.test",
                "phone": "0433 222 118",
                "firm_name": "Northreach Loans",
            },
            {
                "party_type": "accountant",
                "name": "Sandra Bellotti",
                "email": "s.bellotti@bellottiadvisory.test",
                "phone": "03 9000 4422",
                "firm_name": "Bellotti Advisory",
            },
        ],
        "splits": [
            {"amount": d(600_000), "product_type": "variable", "term_months": 360},
            {"amount": d(330_000), "product_type": "fixed_3yr", "term_months": 360},
        ],
    },
    {
        "application_number": "APP-2026-0003",
        "loan_purpose": "investment",
        "applicant_structure": "joint",
        "valuation_amount": d(960_000),
        "valuation_date": "2026-01-15",
        "total_loan_amount": d(672_000),
        "lvr_stated": 0.70,
        "security_address": "22 Bindaree Street, Coorparoo QLD 4151",
        "lodgement_date": "2026-01-22",
        "borrowers": [
            {
                "full_name": "Gemma Halloway",
                "date_of_birth": "1988-09-30",
                "residential_address": "5 Ferndale Avenue, Camp Hill QLD 4152",
                "phone": "0404 777 888",
                "email": "g.halloway@examplemail.test",
                "is_primary": True,
                "employer": "Sunward Retail Group Pty Ltd",
                "employer_abn": "44 555 666 777",
                "fortnightly_gross": d(3_900),
            },
            {
                # Seeded defect 3: no payslips are generated for this borrower.
                "full_name": "Nathan Halloway",
                "date_of_birth": "1986-02-11",
                "residential_address": "5 Ferndale Avenue, Camp Hill QLD 4152",
                "phone": "0404 777 889",
                "email": "n.halloway@examplemail.test",
                "is_primary": False,
                "employer": "Tarragon Facilities Management Pty Ltd",
                "employer_abn": "55 666 777 888",
                "fortnightly_gross": d(4_050),
            },
        ],
        "related_parties": [
            {
                "party_type": "broker",
                "name": "Ines Da Silva",
                "email": "i.dasilva@keystonebrokers.test",
                "phone": "0455 909 121",
                "firm_name": "Keystone Brokers",
            },
        ],
        "splits": [
            {"amount": d(472_000), "product_type": "variable", "term_months": 360},
            {"amount": d(200_000), "product_type": "interest_only", "term_months": 60},
        ],
    },
    {
        "application_number": "APP-2026-0004",
        "loan_purpose": "owner_occupied",
        "applicant_structure": "single",
        "valuation_amount": d(640_000),
        "valuation_date": "2026-01-20",
        "total_loan_amount": d(512_000),
        "lvr_stated": 0.80,
        "security_address": "3/91 Rosslyn Parade, Prospect SA 5082",
        "lodgement_date": "2026-01-27",
        "borrowers": [
            {
                # Seeded defect 4: the KYC document reads "Katherine Ellingham".
                "full_name": "Kathryn Ellingham",
                "date_of_birth": "1991-07-19",
                "residential_address": "3/91 Rosslyn Parade, Prospect SA 5082",
                "phone": "0406 202 303",
                "email": "k.ellingham@examplemail.test",
                "is_primary": True,
                "employer": "Adelaide Civic Institute",
                "employer_abn": "66 777 888 999",
                "fortnightly_gross": d(3_450),
            }
        ],
        "related_parties": [
            {
                "party_type": "broker",
                "name": "Robert Nkemelu",
                "email": "r.nkemelu@southgatefinance.test",
                "phone": "0466 313 414",
                "firm_name": "Southgate Finance",
            },
        ],
        "splits": [
            {"amount": d(512_000), "product_type": "variable", "term_months": 360},
        ],
    },
    {
        "application_number": "APP-2026-0005",
        "loan_purpose": "investment",
        "applicant_structure": "single",
        "valuation_amount": d(1_450_000),
        "valuation_date": "2026-01-23",
        "total_loan_amount": d(1_015_000),
        "lvr_stated": 0.70,
        "security_address": "68 Marlowe Terrace, Cottesloe WA 6011",
        "lodgement_date": "2026-01-30",
        "borrowers": [
            {
                # Seeded defect 5: the payslip YTD figures do not form a sequence.
                "full_name": "Hugo Vandermeer",
                "date_of_birth": "1976-12-02",
                "residential_address": "12 Ridgemont Lane, Claremont WA 6010",
                "phone": "0408 616 717",
                "email": "h.vandermeer@examplemail.test",
                "is_primary": True,
                "employer": "Westline Marine Supplies Pty Ltd",
                "employer_abn": "77 888 999 000",
                "fortnightly_gross": d(6_200),
            }
        ],
        "related_parties": [
            {
                "party_type": "accountant",
                "name": "Adele Marchetti",
                "email": "a.marchetti@marchettico.test",
                "phone": "08 6200 7788",
                "firm_name": "Marchetti and Co",
            },
        ],
        "splits": [
            {"amount": d(615_000), "product_type": "variable", "term_months": 360},
            {"amount": d(250_000), "product_type": "fixed_3yr", "term_months": 360},
            {"amount": d(150_000), "product_type": "interest_only", "term_months": 60},
        ],
    },
    {
        "application_number": "APP-2026-0006",
        "loan_purpose": "owner_occupied",
        "applicant_structure": "joint",
        "valuation_amount": d(820_000),
        "valuation_date": "2026-01-26",
        "total_loan_amount": d(697_000),
        # Seeded defect 6: the true ratio is 0.85. The system of record says 0.78.
        "lvr_stated": 0.78,
        "security_address": "41 Pelham Road, New Town TAS 7008",
        "lodgement_date": "2026-02-02",
        "borrowers": [
            {
                "full_name": "Aroha Whitcombe",
                "date_of_birth": "1993-04-08",
                "residential_address": "41 Pelham Road, New Town TAS 7008",
                "phone": "0409 818 919",
                "email": "a.whitcombe@examplemail.test",
                "is_primary": True,
                "employer": "Derwent Community College",
                "employer_abn": "88 999 000 111",
                "fortnightly_gross": d(3_700),
            },
            {
                "full_name": "Julian Whitcombe",
                "date_of_birth": "1990-10-25",
                "residential_address": "41 Pelham Road, New Town TAS 7008",
                "phone": "0409 818 920",
                "email": "j.whitcombe@examplemail.test",
                "is_primary": False,
                "employer": "Bellerive Freight Services Pty Ltd",
                "employer_abn": "99 000 111 222",
                "fortnightly_gross": d(3_950),
            },
        ],
        "related_parties": [
            {
                "party_type": "solicitor",
                "name": "Harriet Voss",
                "email": "h.voss@vosslegal.test",
                "phone": "03 6100 5533",
                "firm_name": "Voss Legal",
            },
        ],
        "splits": [
            {"amount": d(400_000), "product_type": "variable", "term_months": 360},
            {"amount": d(297_000), "product_type": "fixed_3yr", "term_months": 360},
        ],
    },
    {
        "application_number": "APP-2026-0007",
        "loan_purpose": "investment",
        "applicant_structure": "joint",
        "valuation_amount": d(1_800_000),
        "valuation_date": "2026-01-29",
        "total_loan_amount": d(1_170_000),
        "lvr_stated": 0.65,
        "security_address": "9 Corvina Place, Hunters Hill NSW 2110",
        "lodgement_date": "2026-02-05",
        "borrowers": [
            {
                # Seeded defect 7: this borrower's credit report PDF is written corrupt.
                "full_name": "Beatrix Lindqvist",
                "date_of_birth": "1979-05-14",
                "residential_address": "9 Corvina Place, Hunters Hill NSW 2110",
                "phone": "0410 020 130",
                "email": "b.lindqvist@examplemail.test",
                "is_primary": True,
                "employer": "Aurora Pathology Group Pty Ltd",
                "employer_abn": "10 111 212 313",
                "fortnightly_gross": d(7_400),
            },
            {
                "full_name": "Marcus Lindqvist",
                "date_of_birth": "1977-08-21",
                "residential_address": "9 Corvina Place, Hunters Hill NSW 2110",
                "phone": "0410 020 131",
                "email": "m.lindqvist@examplemail.test",
                "is_primary": False,
                "employer": "Parramatta Trade Fitout Pty Ltd",
                "employer_abn": "20 212 313 414",
                "fortnightly_gross": d(5_150),
            },
        ],
        "related_parties": [
            {
                "party_type": "accountant",
                "name": "Peter Anand",
                "email": "p.anand@anandpartners.test",
                "phone": "02 8300 6611",
                "firm_name": "Anand Partners",
            },
            {
                "party_type": "broker",
                "name": "Lucia Ferrante",
                "email": "l.ferrante@harbourbrokers.test",
                "phone": "0477 424 525",
                "firm_name": "Harbour Brokers",
            },
        ],
        "splits": [
            {"amount": d(700_000), "product_type": "variable", "term_months": 360},
            {"amount": d(270_000), "product_type": "fixed_3yr", "term_months": 360},
            {"amount": d(200_000), "product_type": "interest_only", "term_months": 60},
        ],
    },
    {
        "application_number": "APP-2026-0008",
        "loan_purpose": "investment",
        "applicant_structure": "single",
        "valuation_amount": d(540_000),
        "valuation_date": "2026-02-02",
        "total_loan_amount": d(405_000),
        "lvr_stated": 0.75,
        "security_address": "16 Glenoak Street, Bendigo VIC 3550",
        "lodgement_date": "2026-02-09",
        "borrowers": [
            {
                # Seeded defect 8: the credit report states a DOB of 1984-01-30.
                "full_name": "Simone Achterberg",
                "date_of_birth": "1984-03-01",
                "residential_address": "88 Napier Street, Bendigo VIC 3550",
                "phone": "0412 626 727",
                "email": "s.achterberg@examplemail.test",
                "is_primary": True,
                "employer": "Goldfields Veterinary Practice",
                "employer_abn": "30 313 414 515",
                "fortnightly_gross": d(3_300),
            }
        ],
        "related_parties": [
            {
                "party_type": "broker",
                "name": "Terrence Oyelaran",
                "email": "t.oyelaran@inlandfinance.test",
                "phone": "0488 535 636",
                "firm_name": "Inland Finance",
            },
        ],
        "splits": [
            {"amount": d(255_000), "product_type": "variable", "term_months": 360},
            {"amount": d(150_000), "product_type": "interest_only", "term_months": 60},
        ],
    },
]


def _validate() -> None:
    """Fail loudly on data that would make a defect ambiguous."""
    for app in APPLICATIONS:
        number = app["application_number"]

        split_total = sum(s["amount"] for s in app["splits"])
        if split_total != app["total_loan_amount"]:
            raise ValueError(
                f"{number}: splits sum to {split_total} cents, "
                f"total_loan_amount is {app['total_loan_amount']} cents"
            )

        expected = 1 if app["applicant_structure"] == "single" else 2
        if len(app["borrowers"]) != expected:
            raise ValueError(
                f"{number}: {app['applicant_structure']} application has "
                f"{len(app['borrowers'])} borrowers, expected {expected}"
            )

        if sum(1 for b in app["borrowers"] if b["is_primary"]) != 1:
            raise ValueError(f"{number}: exactly one borrower must be primary")

        if not 1 <= len(app["splits"]) <= 3:
            raise ValueError(f"{number}: splits must number between 1 and 3")


def build() -> None:
    _validate()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    borrower_id = 1
    party_id = 1
    split_id = 1

    for app in APPLICATIONS:
        conn.execute(
            "INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                app["application_number"],
                app["loan_purpose"],
                app["applicant_structure"],
                app["valuation_amount"],
                app["valuation_date"],
                app["total_loan_amount"],
                app["lvr_stated"],
                app["security_address"],
                app["lodgement_date"],
            ),
        )

        for b in app["borrowers"]:
            conn.execute(
                "INSERT INTO borrowers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    borrower_id,
                    app["application_number"],
                    b["full_name"],
                    b["date_of_birth"],
                    b["residential_address"],
                    b["phone"],
                    b["email"],
                    1 if b["is_primary"] else 0,
                ),
            )
            borrower_id += 1

        for p in app["related_parties"]:
            conn.execute(
                "INSERT INTO related_parties VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    party_id,
                    app["application_number"],
                    p["party_type"],
                    p["name"],
                    p["email"],
                    p["phone"],
                    p["firm_name"],
                ),
            )
            party_id += 1

        for n, s in enumerate(app["splits"], start=1):
            conn.execute(
                "INSERT INTO loan_splits VALUES (?, ?, ?, ?, ?, ?)",
                (
                    split_id,
                    app["application_number"],
                    n,
                    s["amount"],
                    s["product_type"],
                    s["term_months"],
                ),
            )
            split_id += 1

    conn.commit()
    conn.close()

    print(
        f"Wrote {len(APPLICATIONS)} applications, {borrower_id - 1} borrowers, "
        f"{party_id - 1} related parties and {split_id - 1} splits to {DB_PATH}"
    )


if __name__ == "__main__":
    build()
