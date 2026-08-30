"""The analysis agent.

Runs the policy evaluator over the evidence ledger and reasons about whether the
evidence is sufficient to evaluate each rule. It computes nothing itself - the
figures were computed by the calculators during the evidence loop and are already
in the ledger with their provenance.

The model's only role here is reasoning about evidence sufficiency: whether what
the ledger holds is enough to evaluate a rule's input. It never decides what a
rule says, and it never decides whether a parameter is met. That is the
evaluator's job and the evaluator is deterministic.

Nothing here forms a view on the deal.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from config import POLICY_PATH
from src.state import MemoState, PolicyFinding
from src.tools.policy import PolicyEvaluator, load_ruleset


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class AnalysisAgent:
    def __init__(self, policy_path=None) -> None:
        self.ruleset = load_ruleset(policy_path or POLICY_PATH)
        self.evaluator = PolicyEvaluator(self.ruleset)

    # -- fact assembly -------------------------------------------------------

    def application_facts(self, state: MemoState) -> dict[str, Any]:
        """Application-level values from the ledger, and only from the ledger.

        A field that is unresolved is absent here rather than defaulted, so its
        rule comes back `not_evaluable` instead of quietly within parameter.
        """
        ledger = state.ledger

        def value(name: str):
            item = ledger.get(name)
            return item.value if item else None

        return {
            "loan_purpose": value("loan_purpose"),
            "applicant_structure": value("applicant_structure"),
            "computed_lvr": _decimal_or_none(value("computed_lvr")),
            "lvr_stated": value("lvr_stated"),
            "valuation_age_days": value("valuation_age_days"),
            "computed_coverage_ratio": _decimal_or_none(value("computed_coverage_ratio")),
            "max_interest_only_term_months": value("max_interest_only_term_months"),
        }

    def identity_status(self, state: MemoState, position: int) -> str | None:
        """One status per borrower, from the verification results.

        A conflict anywhere in a borrower's identity outranks a match elsewhere.
        """
        prefix = f"borrower_{position}_"
        statuses = [
            v.status for v in state.verifications
            if v.field_name.startswith(prefix)
        ]
        if not statuses:
            return None
        if "conflict" in statuses:
            return "conflict"
        if "not_present" in statuses:
            return "not_present"
        return "match"

    def borrower_facts(self, state: MemoState) -> dict[str, dict[str, Any]]:
        facts: dict[str, dict[str, Any]] = {}

        position = 1
        while f"borrower_{position}_record_name" in state.ledger:
            name = state.ledger[f"borrower_{position}_record_name"].value

            def value(suffix: str, position=position):
                item = state.ledger.get(f"borrower_{position}_{suffix}")
                return item.value if item else None

            facts[f"Borrower {position} ({name})"] = {
                "credit_score": value("credit_score"),
                "credit_enquiries_6m": value("credit_enquiries_6m"),
                "credit_defaults_count": value("credit_defaults_count"),
                "identity_verification_status": self.identity_status(state, position),
            }
            position += 1

        return facts

    # -- evidence sufficiency ------------------------------------------------

    def sufficiency_notes(self, state: MemoState, findings: list[PolicyFinding]) -> list[str]:
        """Why each rule that could not be evaluated could not be evaluated.

        Deterministic: it reads the ledger and the unresolved entries. A model is
        not needed to notice that a field is absent, and using one here would put
        a model between the evidence and a policy outcome.
        """
        notes = []
        for finding in findings:
            if finding.status != "not_evaluable":
                continue

            entry = state.unresolved.get(finding.field_name)
            if entry is not None:
                notes.append(
                    f"{finding.rule_id} could not be evaluated: {finding.field_name} is "
                    f"unresolved ({entry.reason})."
                )
            else:
                notes.append(
                    f"{finding.rule_id} could not be evaluated: {finding.field_name} is "
                    f"not in the evidence ledger."
                )
        return notes

    # -- the node ------------------------------------------------------------

    def run(self, state: MemoState) -> list[PolicyFinding]:
        findings = self.evaluator.evaluate(
            self.application_facts(state), self.borrower_facts(state)
        )

        for verification in state.verifications:
            if verification.status != "conflict":
                continue
            findings.append(PolicyFinding(
                rule_id="IDENTITY_SOURCES_AGREE",
                description=(
                    "Identity details in the supporting documents should agree with "
                    "the borrower record."
                ),
                field_name=verification.field_name,
                status="outside_parameter",
                finding_type="discrepancy",
                observed_value=str(verification.document_value.value),
                parameter=f"equal to the record value {verification.record_value.value!r}",
                subject=verification.field_name,
                message=verification.describe(),
            ))

        return findings


def analysis_node(state: MemoState, agent: AnalysisAgent | None = None) -> dict:
    agent = agent or AnalysisAgent()
    findings = agent.run(state)

    counts = {"within_parameter": 0, "outside_parameter": 0, "not_evaluable": 0}
    for finding in findings:
        counts[finding.status] += 1

    trace = list(state.trace)
    trace.append(
        f"Policy ruleset {agent.ruleset.version}: {counts['within_parameter']} within "
        f"parameter, {counts['outside_parameter']} outside, {counts['not_evaluable']} "
        f"not evaluable."
    )
    trace.extend(agent.sufficiency_notes(state, findings))

    return {"policy_findings": findings, "trace": trace}
