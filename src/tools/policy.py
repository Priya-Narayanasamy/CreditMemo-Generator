"""Deterministic evaluation of the credit policy ruleset.

Rules are data loaded from YAML. No rule text ever reaches a prompt, and no model
takes any part in deciding whether a rule's parameter is met. A model's only role
near policy is reasoning about whether the evidence needed to evaluate a rule is
present at all, and that happens in the analysis agent, not here.

Nothing in this module produces a verdict on a deal. A rule whose parameter is not
met yields `outside_parameter` with a `discrepancy` or `note` for the analyst.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from src.state import FindingType, PolicyFinding


class PolicyRule(BaseModel):
    rule_id: str
    description: str
    field: str
    operator: str
    finding_type: FindingType
    applies_to: dict[str, str] = Field(default_factory=dict)
    threshold: Any = None
    compare_to: str | None = None
    tolerance: str | None = None
    per_borrower: bool = False


class PolicyRuleset(BaseModel):
    version: str
    effective_from: date
    rates: dict[str, str]
    assessment_rate_buffer: str
    rules: list[PolicyRule]

    @property
    def decimal_rates(self) -> dict[str, Decimal]:
        return {name: Decimal(value) for name, value in self.rates.items()}

    @property
    def buffer(self) -> Decimal:
        return Decimal(self.assessment_rate_buffer)

    def rule(self, rule_id: str) -> PolicyRule:
        for candidate in self.rules:
            if candidate.rule_id == rule_id:
                return candidate
        raise KeyError(rule_id)


def load_ruleset(path: Path) -> PolicyRuleset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    ruleset = PolicyRuleset.model_validate(raw)

    seen: set[str] = set()
    for rule in ruleset.rules:
        if rule.rule_id in seen:
            raise ValueError(f"duplicate rule_id {rule.rule_id!r} in {path}")
        seen.add(rule.rule_id)
        if rule.operator not in OPERATORS:
            raise ValueError(
                f"rule {rule.rule_id!r} uses unknown operator {rule.operator!r}"
            )
        if rule.operator == "equals_field":
            if not rule.compare_to:
                raise ValueError(f"rule {rule.rule_id!r} needs a compare_to field")
        elif rule.threshold is None:
            raise ValueError(f"rule {rule.rule_id!r} needs a threshold")

    return ruleset


# --- Operators --------------------------------------------------------------
# Each returns (parameter_met, rendered_parameter).

def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    if isinstance(value, float):
        # Floats reach here only from the `lvr_stated` column, which is REAL in the
        # system of record. Convert through str so the comparison is exact to the
        # digits actually recorded rather than to the binary expansion.
        return Decimal(str(value))
    return None


def _numeric_compare(observed: Any, threshold: Any, compare: Callable[[Decimal, Decimal], bool]):
    left, right = _as_decimal(observed), _as_decimal(threshold)
    if left is None or right is None:
        raise TypeError(f"cannot compare {observed!r} with {threshold!r} numerically")
    return compare(left, right)


OPERATORS: dict[str, Callable] = {
    "lte": lambda o, t: _numeric_compare(o, t, lambda a, b: a <= b),
    "lt": lambda o, t: _numeric_compare(o, t, lambda a, b: a < b),
    "gte": lambda o, t: _numeric_compare(o, t, lambda a, b: a >= b),
    "gt": lambda o, t: _numeric_compare(o, t, lambda a, b: a > b),
    "eq": lambda o, t: (
        _numeric_compare(o, t, lambda a, b: a == b)
        if _as_decimal(o) is not None and _as_decimal(t) is not None
        else str(o) == str(t)
    ),
    "ne": lambda o, t: (
        _numeric_compare(o, t, lambda a, b: a != b)
        if _as_decimal(o) is not None and _as_decimal(t) is not None
        else str(o) != str(t)
    ),
    "equals_field": None,   # handled separately; needs a second observed value
}


class PolicyEvaluator:
    """Evaluates a ruleset against a flat mapping of field name to value.

    The caller assembles `facts` from the evidence ledger. A field absent from
    `facts`, or present with a value of None, makes its rule `not_evaluable` -
    never silently within parameter.
    """

    def __init__(self, ruleset: PolicyRuleset) -> None:
        self.ruleset = ruleset

    def applies(self, rule: PolicyRule, facts: dict[str, Any]) -> bool:
        return all(str(facts.get(key)) == str(value) for key, value in rule.applies_to.items())

    def evaluate(
        self,
        facts: dict[str, Any],
        borrower_facts: dict[str, dict[str, Any]] | None = None,
    ) -> list[PolicyFinding]:
        """Evaluate every applicable rule.

        `facts` holds application-level values. `borrower_facts` maps a borrower
        label to that borrower's values, for rules marked `per_borrower`.
        """
        findings: list[PolicyFinding] = []

        for rule in self.ruleset.rules:
            if not self.applies(rule, facts):
                continue

            if rule.per_borrower:
                for subject, subject_facts in (borrower_facts or {}).items():
                    findings.append(self._evaluate_one(rule, subject_facts, facts, subject))
            else:
                findings.append(self._evaluate_one(rule, facts, facts, None))

        return findings

    def _evaluate_one(
        self,
        rule: PolicyRule,
        scope: dict[str, Any],
        application_facts: dict[str, Any],
        subject: str | None,
    ) -> PolicyFinding:
        observed = scope.get(rule.field)

        if observed is None:
            return PolicyFinding(
                rule_id=rule.rule_id,
                description=rule.description,
                field_name=rule.field,
                status="not_evaluable",
                finding_type="missing",
                observed_value=None,
                parameter=self._render_parameter(rule, application_facts),
                subject=subject,
                message=(
                    f"{rule.field} is not in the evidence ledger, so this rule could "
                    f"not be evaluated."
                ),
            )

        if rule.operator == "equals_field":
            return self._evaluate_field_comparison(rule, scope, application_facts, subject, observed)

        try:
            met = OPERATORS[rule.operator](observed, rule.threshold)
        except TypeError as exc:
            return PolicyFinding(
                rule_id=rule.rule_id,
                description=rule.description,
                field_name=rule.field,
                status="not_evaluable",
                finding_type="missing",
                observed_value=str(observed),
                parameter=self._render_parameter(rule, application_facts),
                subject=subject,
                message=str(exc),
            )

        return self._finding(rule, met, observed, application_facts, subject)

    def _evaluate_field_comparison(
        self,
        rule: PolicyRule,
        scope: dict[str, Any],
        application_facts: dict[str, Any],
        subject: str | None,
        observed: Any,
    ) -> PolicyFinding:
        other = scope.get(rule.compare_to, application_facts.get(rule.compare_to))

        if other is None:
            return PolicyFinding(
                rule_id=rule.rule_id,
                description=rule.description,
                field_name=rule.field,
                status="not_evaluable",
                finding_type="missing",
                observed_value=str(observed),
                parameter=f"equal to {rule.compare_to}",
                subject=subject,
                message=(
                    f"{rule.compare_to} is not in the evidence ledger, so "
                    f"{rule.field} could not be compared against it."
                ),
            )

        left, right = _as_decimal(observed), _as_decimal(other)
        tolerance = Decimal(rule.tolerance) if rule.tolerance else Decimal(0)

        if left is None or right is None:
            met = str(observed) == str(other)
        else:
            met = abs(left - right) <= tolerance

        finding = self._finding(rule, met, observed, application_facts, subject)
        finding.parameter = f"equal to {rule.compare_to} ({other})"
        if not met:
            finding.message = (
                f"{rule.field} is {observed}, while {rule.compare_to} is {other}. "
                f"The two sources disagree and the difference has not been resolved."
            )
        return finding

    def _finding(
        self,
        rule: PolicyRule,
        met: bool,
        observed: Any,
        application_facts: dict[str, Any],
        subject: str | None,
    ) -> PolicyFinding:
        parameter = self._render_parameter(rule, application_facts)
        return PolicyFinding(
            rule_id=rule.rule_id,
            description=rule.description,
            field_name=rule.field,
            status="within_parameter" if met else "outside_parameter",
            finding_type="note" if met else rule.finding_type,
            observed_value=str(observed),
            parameter=parameter,
            subject=subject,
            message=(
                f"{rule.field} is {observed}, {parameter}."
                if met
                else f"{rule.field} is {observed}; the policy parameter is {parameter}."
            ),
        )

    @staticmethod
    def _render_parameter(rule: PolicyRule, facts: dict[str, Any]) -> str:
        words = {
            "lte": "at most",
            "lt": "below",
            "gte": "at least",
            "gt": "above",
            "eq": "equal to",
            "ne": "not equal to",
            "equals_field": "equal to",
        }
        if rule.operator == "equals_field":
            return f"equal to {rule.compare_to}"
        return f"{words[rule.operator]} {rule.threshold}"


def summarise(findings: list[PolicyFinding]) -> dict[str, int]:
    """Counts by status. Used by the UI and the memo's policy table."""
    counts = {"within_parameter": 0, "outside_parameter": 0, "not_evaluable": 0}
    for finding in findings:
        counts[finding.status] += 1
    return counts
