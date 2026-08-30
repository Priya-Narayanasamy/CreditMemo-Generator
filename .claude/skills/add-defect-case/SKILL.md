---
name: add-defect-case
description: Add a deliberately broken application to the synthetic data set, along with the end-to-end test asserting how the agent handles it. Use when adding a new failure mode, when testing error handling or escalation behaviour, or when a bug is found in production-like behaviour and should be pinned by a reproducible case.
---

# Add a defect case

Seeded defects are how error handling gets built rather than bolted on. Each defect is
a broken application in the synthetic data plus a test asserting the expected behaviour.

## 1. Classify the defect

Every defect falls into one of four categories, and the category determines the expected
behaviour. Decide this before writing any code.

| Category | Example | Expected behaviour |
|---|---|---|
| **Missing** | A document type absent entirely | Retry within per-field budget, then escalate as `missing` |
| **Conflict** | KYC name differs from the application record | Immediate escalation as `discrepancy`, no retry |
| **Unreadable** | Corrupt or image-only PDF | Parse failure returned into state, retry with different settings, then escalate |
| **Internally inconsistent** | Payslip year-to-date figures that do not form a sequence | Conflict on the derived field, escalation, no averaging or reconciliation |

If a proposed defect does not fit a category, that is worth raising — it may be
revealing a gap in the state model rather than a new test case.

## 2. Break the data

In `data/generate_docs.py` or `data/generate_db.py`, make the defect **deterministic**.
It must reproduce identically on every regeneration — no randomness in what is broken
or how.

Break exactly one thing per application. An application with two defects cannot tell
you which one caused an escalation.

## 3. Record it

Add an entry to `data/defects.json`:

```json
{
  "application_number": "APP-2026-0009",
  "category": "conflict",
  "description": "Equifax report subject address differs from borrower record",
  "affected_fields": ["borrower_residential_address"],
  "expected_outcome": "escalation",
  "expected_reason": "conflict"
}
```

Tests read from this file. Never hard-code the defect details in the test itself.

## 4. Write the end-to-end test

In `tests/test_defects.py`, run the graph against the application and assert:

- The run halts at the expected interrupt type (`ESCALATION`, not `APPROVAL`)
- The affected field appears in `state.unresolved` with the expected reason
- For conflicts: `conflicting_values` holds both values, each with its own provenance
- For missing: attempt count is within the per-field budget, and other fields'
  budgets were not consumed
- No memo was written

Also assert the negative: fields **unaffected** by the defect resolved normally. A
defect that halts the whole run rather than isolating to its field is a bug.

## 5. Check the escalation message

An escalation is a message to a human analyst. Assert it states which field could not
be resolved, which sources were tried, and what each returned. "Extraction failed" is
not an acceptable escalation.

## Checks before finishing

- Is the defect deterministic across regenerations?
- Is exactly one thing broken in this application?
- Does the test read from `defects.json` rather than hard-coding?
- Does the test assert the negative — that unaffected fields still resolved?
- Does the escalation carry enough detail for an analyst to act on it?
