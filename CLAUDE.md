# Credit Memo Agent

A multi-agent system that produces a **first-draft credit memo** consolidating a loan
application file. All data in this repository is synthetic.

## What this system is

A verification and consolidation tool for a credit analyst. It:

- Cross-checks identity details in supporting documents against the application record
- Extracts income from payslips and credit history from the credit report
- Consolidates everything relevant to one application into a single document
- Surfaces every discrepancy, gap and missing document it finds

## What this system is NOT

**This is not a credit decisioning engine.** It does not assess, approve, decline, rate
or recommend. This is the single most important constraint in the repository.

Concretely, never write code or content that:

- Produces an approval, decline, or recommendation
- Assigns a risk grade, risk score, or credit rating
- Labels a finding as pass, fail, breach, or hard fail
- Describes a deal as strong, weak, acceptable, or marginal
- Concludes that a borrower can or cannot service a loan

Findings use the vocabulary `discrepancy`, `missing`, `note`. The memo presents what
the file contains and where sources disagree. The analyst forms the credit view.

If a task seems to require an assessment output, stop and ask rather than inventing one.

---

## Hard rules

### Provenance
- No figure may appear in the memo unless it exists in the evidence ledger with a
  resolved source. There is no exception to this.
- Every ledger entry carries a `Provenance`: database table and column, or filename
  and page and document type, or the ledger fields it was computed from.
- If a value cannot be sourced, escalate. Never estimate, infer, or leave a plausible
  placeholder.

### Money and numbers
- Currency is integer cents throughout. Never float.
- Ratios use `Decimal`. Never float.
- No LLM performs arithmetic. All calculation lives in `tools/calculators.py` as pure
  Python functions with unit tests.

### Determinism
- Structure, headings, tables, labels and conditional boilerplate come from Jinja2
  templates, not from a model.
- Model calls are limited to: document classification, field extraction, source
  selection in the evidence loop, narrative sections, and review.
- All model calls at temperature 0 with pinned version strings from `config.py`.
- Once `approved_memo` is set in state, the drafting node must not run again.

### Gaps versus conflicts
- A gap is a field with no value found. It is retried within a **per-field** budget.
- A conflict is two sources giving different values for the same field. It goes
  straight to escalation and does not consume retry budget — retrying cannot resolve it.
- Store the outcome of each attempt, not just a count. Never retry a document that
  parsed cleanly and did not contain the field.

### Human boundary
- Two distinct interrupt types, kept separate in state: `ESCALATION` (cannot proceed)
  and `APPROVAL` (finished, requesting permission to write).
- No write of any kind before approval.

### Data
- Synthetic only. No real names, institutions, branding, document numbers or ABNs.
- Generated documents carry a `SAMPLE — SYNTHETIC DATA` watermark.
- Never commit `.env`. Never print or log an API key.

---

## Conventions

- Python 3.11+, type hints everywhere, Pydantic v2 for all structured data
- No vector store — targeted extraction against classified documents only
- Parsing sits behind the `DocumentParser` protocol in `tools/parsing.py`. Never call
  a parsing library directly from an agent.
- A tool failure is a value returned into state, not an exception that escapes the node
- Log every model call (prompt, response, token counts) to `logs/model_calls.jsonl`
- Tests live in `tests/`, run with `pytest`

## Commands

```
python data/generate_db.py        # rebuild the synthetic database
python data/generate_docs.py      # rebuild the synthetic PDFs
python -m pytest                  # full test suite
python -m streamlit run app.py    # the UI
```

## Working style

- Stop at phase boundaries in the build spec for review
- Get application `APP-2026-0001` working end to end before touching the seeded defects
- When a rule here conflicts with something asked in conversation, raise it rather
  than silently picking one
