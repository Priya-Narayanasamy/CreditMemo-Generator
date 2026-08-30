"""Phase 2 tests: the policy evaluator.

The evaluator is deterministic and no model touches it. These tests cover a rule
whose parameter is met, one whose parameter is not, one that cannot be evaluated
because the evidence is absent, and the per-borrower and field-comparison forms.

They also pin the vocabulary: no finding may carry a pass, fail, breach, grade or
recommendation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from config import POLICY_PATH
from src.tools.policy import PolicyEvaluator, PolicyRuleset, load_ruleset, summarise


@pytest.fixture(scope="module")
def ruleset() -> PolicyRuleset:
    return load_ruleset(POLICY_PATH)


@pytest.fixture(scope="module")
def evaluator(ruleset) -> PolicyEvaluator:
    return PolicyEvaluator(ruleset)


def finding_for(findings, rule_id, subject=None):
    return next(f for f in findings if f.rule_id == rule_id and f.subject == subject)


# --- The ruleset itself -----------------------------------------------------


def test_ruleset_loads_and_pins_its_version(ruleset):
    assert ruleset.version == "v1"
    assert ruleset.rules


def test_rates_and_buffer_are_decimal_never_float(ruleset):
    assert all(isinstance(rate, Decimal) for rate in ruleset.decimal_rates.values())
    assert isinstance(ruleset.buffer, Decimal)
    assert {"variable", "fixed_3yr", "interest_only"} <= set(ruleset.decimal_rates)


def test_every_rule_declares_a_neutral_finding_type(ruleset):
    for rule in ruleset.rules:
        assert rule.finding_type in {"discrepancy", "missing", "note"}, rule.rule_id


def test_ruleset_carries_no_assessment_language():
    """CLAUDE.md forbids pass/fail/breach vocabulary. The spec's example rule used
    `severity: hard_fail`; this repository does not."""
    raw = POLICY_PATH.read_text(encoding="utf-8").lower()
    banned = ["hard_fail", "soft_fail", "severity", "approve", "decline",
              "risk grade", "risk score", "breach"]

    for word in banned:
        assert word not in raw, f"policy ruleset contains banned vocabulary {word!r}"


def test_loader_rejects_a_duplicate_rule_id(tmp_path):
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    raw["rules"].append(dict(raw["rules"][0]))
    path = tmp_path / "dupe.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate rule_id"):
        load_ruleset(path)


def test_loader_rejects_an_unknown_operator(tmp_path):
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    raw["rules"][0]["operator"] = "roughly"
    path = tmp_path / "bad-operator.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown operator"):
        load_ruleset(path)


# --- Evaluation -------------------------------------------------------------


def base_facts(**overrides):
    facts = {
        "loan_purpose": "owner_occupied",
        "computed_lvr": Decimal("0.7500"),
        "lvr_stated": 0.75,
        "valuation_age_days": 6,
        "computed_coverage_ratio": Decimal("2.1000"),
        "max_interest_only_term_months": 0,
    }
    facts.update(overrides)
    return facts


def base_borrowers(**overrides):
    borrower = {
        "credit_score": 812,
        "credit_enquiries_6m": 1,
        "credit_defaults_count": 0,
        "identity_verification_status": "match",
    }
    borrower.update(overrides)
    return {"Borrower 1": borrower}


def test_a_met_parameter_is_within_parameter(evaluator):
    findings = evaluator.evaluate(base_facts(), base_borrowers())
    lvr = finding_for(findings, "LVR_MAX_OO")

    assert lvr.status == "within_parameter"
    assert lvr.finding_type == "note"
    assert lvr.observed_value == "0.7500"


def test_an_unmet_parameter_is_outside_parameter_with_the_declared_finding_type(evaluator):
    findings = evaluator.evaluate(base_facts(computed_lvr=Decimal("0.8500")), base_borrowers())
    lvr = finding_for(findings, "LVR_MAX_OO")

    assert lvr.status == "outside_parameter"
    assert lvr.finding_type == "discrepancy"
    assert "0.85" in lvr.observed_value


def test_an_absent_field_is_not_evaluable_never_silently_within_parameter(evaluator):
    facts = base_facts()
    del facts["computed_coverage_ratio"]

    findings = evaluator.evaluate(facts, base_borrowers())
    serviceability = finding_for(findings, "SERVICEABILITY_MIN_COVERAGE")

    assert serviceability.status == "not_evaluable"
    assert serviceability.finding_type == "missing"
    assert "not in the evidence ledger" in serviceability.message


def test_a_none_value_is_not_evaluable(evaluator):
    findings = evaluator.evaluate(base_facts(computed_coverage_ratio=None), base_borrowers())

    assert finding_for(findings, "SERVICEABILITY_MIN_COVERAGE").status == "not_evaluable"


def test_applies_to_selects_the_right_rule(evaluator):
    owner_occupied = evaluator.evaluate(base_facts(), base_borrowers())
    investment = evaluator.evaluate(base_facts(loan_purpose="investment"), base_borrowers())

    assert {f.rule_id for f in owner_occupied} >= {"LVR_MAX_OO"}
    assert "LVR_MAX_INV" not in {f.rule_id for f in owner_occupied}
    assert "LVR_MAX_OO" not in {f.rule_id for f in investment}
    assert "LVR_MAX_INV" in {f.rule_id for f in investment}


def test_field_comparison_detects_a_stated_lvr_disagreement(evaluator):
    """Seeded defect 6: the record says 0.78, the calculation says 0.85."""
    findings = evaluator.evaluate(
        base_facts(computed_lvr=Decimal("0.8500"), lvr_stated=0.78), base_borrowers()
    )
    agreement = finding_for(findings, "LVR_AGREES_WITH_RECORD")

    assert agreement.status == "outside_parameter"
    assert agreement.finding_type == "discrepancy"
    assert "0.78" in agreement.message and "0.85" in agreement.message


def test_field_comparison_tolerates_float_representation_of_the_record(evaluator):
    findings = evaluator.evaluate(base_facts(computed_lvr=Decimal("0.7000"), lvr_stated=0.7),
                                  base_borrowers())

    assert finding_for(findings, "LVR_AGREES_WITH_RECORD").status == "within_parameter"


def test_per_borrower_rules_evaluate_once_per_borrower(evaluator):
    borrowers = {
        "Borrower 1": dict(credit_score=812, credit_enquiries_6m=1,
                           credit_defaults_count=0, identity_verification_status="match"),
        "Borrower 2": dict(credit_score=540, credit_enquiries_6m=8,
                           credit_defaults_count=1, identity_verification_status="match"),
    }
    findings = evaluator.evaluate(base_facts(), borrowers)

    assert finding_for(findings, "MIN_CREDIT_SCORE", "Borrower 1").status == "within_parameter"
    assert finding_for(findings, "MIN_CREDIT_SCORE", "Borrower 2").status == "outside_parameter"
    assert finding_for(findings, "MAX_ENQUIRIES_6M", "Borrower 2").status == "outside_parameter"


def test_a_listed_default_produces_a_note_not_a_discrepancy(evaluator):
    """Nathan Halloway carries one listed default. It is recorded for the analyst,
    and it does not halt anything."""
    findings = evaluator.evaluate(base_facts(), base_borrowers(credit_defaults_count=1))
    defaults = finding_for(findings, "NO_LISTED_DEFAULTS", "Borrower 1")

    assert defaults.status == "outside_parameter"
    assert defaults.finding_type == "note"


def test_identity_mismatch_is_a_discrepancy(evaluator):
    findings = evaluator.evaluate(
        base_facts(), base_borrowers(identity_verification_status="conflict")
    )

    assert finding_for(findings, "IDENTITY_VERIFIED", "Borrower 1").finding_type == "discrepancy"


def test_evaluation_is_deterministic(evaluator):
    facts, borrowers = base_facts(), base_borrowers()
    runs = [evaluator.evaluate(facts, borrowers) for _ in range(10)]

    serialised = {tuple((f.rule_id, f.subject, f.status, f.observed_value) for f in run) for run in runs}
    assert len(serialised) == 1


def test_no_finding_carries_assessment_language(evaluator):
    findings = evaluator.evaluate(
        base_facts(computed_lvr=Decimal("0.9500"), computed_coverage_ratio=Decimal("0.4000")),
        base_borrowers(credit_score=410, identity_verification_status="conflict"),
    )
    banned = ["approve", "decline", "reject", " fail", "failed", "pass", "breach",
              "risk grade", "strong", "weak", "marginal", "recommend", "unacceptable"]

    for finding in findings:
        text = f"{finding.message} {finding.description}".lower()
        for word in banned:
            assert word not in text, f"{finding.rule_id}: {word!r} in {text!r}"


def test_summarise_counts_by_status(evaluator):
    findings = evaluator.evaluate(base_facts(computed_lvr=Decimal("0.8500")), base_borrowers())
    counts = summarise(findings)

    assert sum(counts.values()) == len(findings)
    assert counts["outside_parameter"] >= 1
