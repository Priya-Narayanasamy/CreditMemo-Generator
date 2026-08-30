---
name: add-evidence-field
description: Add a new field to the evidence ledger end to end — extraction schema, source resolution, template slot, and tests. Use when a new piece of information needs to appear in the credit memo, when a memo section is missing data, or when asked to capture something from a document or the application database that is not currently extracted.
---

# Add an evidence field

Adding a field touches five places. Missing any one of them produces a field that
either never populates or populates without provenance. Work through all five.

## 1. Decide the source

Every field resolves from exactly one kind of source:

- **database** — a column in `applications`, `borrowers`, `related_parties`, `loan_splits`
- **document** — a specific document type (`equifax`, `kyc`, `payslip`)
- **computed** — derived from other ledger fields by a pure function

If a field could come from more than one source, that is a verification field. It needs
**both** sources resolved and compared, not one picked. See section 6.

## 2. Declare it as required

Add the field name to the template's required-fields list. The evidence agent derives
what it must find from this list — a field not declared here will never be sought.

Mark whether absence is an escalation or an acceptable omission. Not every field is
mandatory for every template.

## 3. Extraction

**Database fields:** add to the relevant query function in `tools/database.py`. The
return type must carry enough information to build a `Provenance` with table and column.

**Document fields:** add to the Pydantic extraction schema for that document type in
`tools/extraction.py`. The field must be `| None` — returning `None` when absent is
required behaviour. Never make a field non-optional to force the model to produce it.

**Computed fields:** add a pure function to `tools/calculators.py`. No LLM. The
`Provenance` lists the ledger fields it consumed.

## 4. Template slot

Add the slot to the relevant Jinja2 templates. Check all four — a field added to only
one template silently disappears from the others.

Prefer a slot-filled sentence in the template over asking the drafting agent to mention
the value in narrative. Narrative should not carry figures that a template can place.

## 5. Tests

Three tests minimum:

- The field populates correctly for `APP-2026-0001`
- The field's absence produces the declared behaviour (escalation or omission)
- The field appears in the rendered memo with intact provenance

## 6. Verification fields

If the field exists in both the application record and a document, it is a verification
field. It resolves to a comparison, not a value:

- Resolve the database value and the document value independently
- Both carry their own `Provenance`
- Equal values produce a `match` status
- Unequal values produce a `conflict` — which escalates immediately and does not consume
  retry budget
- One side missing produces `not_present`, not a conflict

Add a row to the verification matrix section of the templates. Comparison logic is a
pure function, never a model call.

## Checks before finishing

- Does the field have a `Provenance` in every path that can set it?
- Is there any code path where the field reaches the memo without one?
- Does the extraction schema allow `None`?
- Are all four templates updated?
- Does the vocabulary avoid assessment language? Findings are `discrepancy`, `missing`
  or `note` — never pass, fail, breach, or any judgement of the deal.
