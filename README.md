# Credit Memo Agent

A multi-agent system that produces a **first-draft credit memo** consolidating a loan
application file, for a non-bank lender. All data in this repository is synthetic.

This is a verification and consolidation tool for a credit analyst. It cross-checks
identity details across documents, extracts income and credit history, consolidates
everything relevant to one application, and surfaces every discrepancy and gap it finds.

**It is not a credit decisioning engine.** It does not assess, approve, decline, rate or
recommend. Findings use the vocabulary `discrepancy`, `missing`, `note`. The analyst
forms the credit view.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in the keys
```

## Commands

```
python data/generate_db.py        # rebuild the synthetic database
python data/generate_docs.py      # rebuild the synthetic PDFs and defects.json
pytest                            # full test suite
streamlit run app.py              # the UI
```

## Build status

| Phase | Status |
|---|---|
| 1. Synthetic data (database, documents, seeded defects) | done |
| 2. State, evidence ledger, calculators, policy evaluator | not started |
| 3. Tools: database, documents, parsing, extraction | not started |
| 4. Evidence agent | not started |
| 5. Analysis, drafting, review agents | not started |
| 6. Graph assembly with both interrupt types | not started |
| 7. Templates and renderer | not started |
| 8. Streamlit UI | not started |
| 9. Error-handling hardening against the seeded defects | not started |

## The synthetic data set

Eight applications, `APP-2026-0001` through `APP-2026-0008`: two owner-occupied single,
two owner-occupied joint, two investment single, two investment joint. Money is stored
as integer cents throughout. Both generators are fully deterministic - regenerating
produces an identical data set.

Each borrower has a bureau-style credit report, an identity verification record, and
three consecutive fortnightly payslips. Documents are text-extractable PDFs with real
table structure, watermarked `SAMPLE - SYNTHETIC DATA`. Filenames are deliberately
unhelpful and decorrelated from document type - the agent classifies from content.

Nothing imitates a real institution. Names, addresses, employers, providers, reference
numbers and identification numbers are all invented.

### Seeded defects

Exactly one thing is broken per application, recorded in `data/defects.json` along with
a manifest of what each unhelpfully named file actually is. Tests read their
expectations from that file rather than hard-coding them.

| Application | Category | Defect |
|---|---|---|
| 0001 | clean | Happy path, single applicant |
| 0002 | clean | Happy path, joint applicants |
| 0003 | missing | No payslips at all for the second applicant |
| 0004 | conflict | KYC names the subject `Katherine`; the record says `Kathryn` |
| 0005 | internally inconsistent | Payslip year-to-date gross figures do not form a sequence |
| 0006 | conflict | `lvr_stated` is 0.78; loan / valuation is 0.85 |
| 0007 | unreadable | One credit report is a corrupt, unparseable PDF |
| 0008 | conflict | Credit report subject DOB disagrees with the borrower record |
