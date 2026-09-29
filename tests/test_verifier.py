"""Tests for Stage 4: Verifier (pure code, no LLM)."""

import pytest

from src.expense_pipeline.schemas import Expense, PolicyRule, PolicyRules
from src.expense_pipeline.verifier import verify_compliance


def create_test_expenses():
    """Create test expense data."""
    return [
        Expense(
            report_id="EXP-0001",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=50.00,
            currency="USD",
            receipt_attached=True,
        ),
        Expense(
            report_id="EXP-0002",
            employee="Bob",
            department="Sales",
            date="2024-01-15",
            category="meals",
            amount=100.00,
            currency="USD",
            receipt_attached=False,
        ),
        Expense(
            report_id="EXP-0003",
            employee="Carol",
            department="Finance",
            date="2024-01-15",
            category="software",
            amount=2000.00,
            currency="USD",
            receipt_attached=True,
        ),
    ]


def create_test_policy():
    """Create test policy rules."""
    rules = {
        "meals": PolicyRule(
            category="meals",
            daily_limit=75.00,
            receipt_required_above=30.00,
        ),
        "travel": PolicyRule(
            category="travel",
            daily_limit=500.00,
            receipt_required_above=150.00,
        ),
        "software": PolicyRule(
            category="software",
            daily_limit=1500.00,
            receipt_required_above=500.00,
            requires_manager_approval_above=750.00,
        ),
    }
    return PolicyRules(rules=rules, source_file="test.yaml")


def test_verify_compliance_approved_expense():
    """Test verification of an expense that should be approved."""
    expenses = [
        Expense(
            report_id="EXP-0001",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=50.00,
            currency="USD",
            receipt_attached=True,
        )
    ]
    policy = create_test_policy()

    output = verify_compliance(expenses, policy)

    assert len(output.verdicts) == 1
    assert output.verdicts[0].report_id == "EXP-0001"
    assert output.verdicts[0].verdict == "approved"
    assert len(output.verdicts[0].reasons) == 0


def test_verify_compliance_exceeds_daily_limit():
    """Test verification of an expense exceeding daily limit."""
    expenses = [
        Expense(
            report_id="EXP-0002",
            employee="Bob",
            department="Sales",
            date="2024-01-15",
            category="meals",
            amount=100.00,
            currency="USD",
            receipt_attached=True,
        )
    ]
    policy = create_test_policy()

    output = verify_compliance(expenses, policy)

    assert len(output.verdicts) == 1
    assert output.verdicts[0].verdict == "flagged"
    assert "daily limit" in " ".join(output.verdicts[0].reasons).lower()


def test_verify_compliance_missing_receipt():
    """Test verification of an expense missing required receipt."""
    expenses = [
        Expense(
            report_id="EXP-0003",
            employee="Carol",
            department="Finance",
            date="2024-01-15",
            category="meals",
            amount=75.00,
            currency="USD",
            receipt_attached=False,
        )
    ]
    policy = create_test_policy()

    output = verify_compliance(expenses, policy)

    assert len(output.verdicts) == 1
    assert output.verdicts[0].verdict == "flagged"
    assert "receipt" in " ".join(output.verdicts[0].reasons).lower()


def test_verify_compliance_manager_approval_required():
    """Test verification of software expense requiring manager approval."""
    expenses = [
        Expense(
            report_id="EXP-0004",
            employee="Dave",
            department="Engineering",
            date="2024-01-15",
            category="software",
            amount=1000.00,
            currency="USD",
            receipt_attached=True,
        )
    ]
    policy = create_test_policy()

    output = verify_compliance(expenses, policy)

    assert len(output.verdicts) == 1
    assert output.verdicts[0].verdict == "flagged"
    assert "manager approval" in " ".join(output.verdicts[0].reasons).lower()


def test_verify_compliance_row_count_parity():
    """Test that verifier preserves row count."""
    expenses = create_test_expenses()
    policy = create_test_policy()

    output = verify_compliance(expenses, policy)

    assert len(output.verdicts) == len(expenses)


def test_daily_limit_sums_same_employee_day_category_and_currency():
    """Two under-limit meals on the same day exceed the daily limit together."""
    policy = create_test_policy()
    expenses = [
        Expense(
            report_id="EXP-0001",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=40.00,
            currency="USD",
            receipt_attached=True,
        ),
        Expense(
            report_id="EXP-0002",
            employee="Alice",
            department="Engineering",
            date="2024-01-15 00:00:00",
            category="Meals",
            amount=40.00,
            currency="usd",
            receipt_attached=True,
        ),
    ]

    output = verify_compliance(expenses, policy)

    assert [verdict.verdict for verdict in output.verdicts] == ["flagged", "flagged"]
    for verdict in output.verdicts:
        daily = next(check for check in verdict.checks if check["clause"] == "meals.daily_limit")
        assert daily["observed_value"] == 80.00
        assert daily["result"] == "fail"
        assert daily["contributing_report_ids"] == ["EXP-0001", "EXP-0002"]
        assert "meals.daily_limit" in verdict.rule_citations
        assert "meals.receipt_required_above" in verdict.rule_citations


def test_daily_limit_does_not_combine_currencies_or_other_days():
    policy = create_test_policy()
    expenses = [
        Expense(
            report_id="EXP-USD",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=50.00,
            currency="USD",
            receipt_attached=True,
        ),
        Expense(
            report_id="EXP-EUR",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=50.00,
            currency="EUR",
            receipt_attached=True,
        ),
        Expense(
            report_id="EXP-NEXT",
            employee="Alice",
            department="Engineering",
            date="2024-01-16",
            category="meals",
            amount=50.00,
            currency="USD",
            receipt_attached=True,
        ),
    ]

    output = verify_compliance(expenses, policy)
    by_id = {verdict.report_id: verdict for verdict in output.verdicts}

    assert by_id["EXP-USD"].verdict == "approved"
    assert by_id["EXP-EUR"].verdict == "approved"
    assert by_id["EXP-NEXT"].verdict == "approved"
    assert by_id["EXP-USD"].checks[0]["observed_value"] == 50.00
    assert by_id["EXP-EUR"].checks[0]["observed_currency"] == "EUR"


def test_approved_expense_cites_every_applied_clause():
    expenses = [
        Expense(
            report_id="EXP-0001",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=50.00,
            currency="USD",
            receipt_attached=True,
        )
    ]
    output = verify_compliance(expenses, create_test_policy())
    verdict = output.verdicts[0]
    assert verdict.verdict == "approved"
    assert verdict.rule_citations == [
        "meals.daily_limit",
        "meals.receipt_required_above",
    ]
    assert [check["result"] for check in verdict.checks] == ["pass", "pass"]


def test_verify_compliance_never_depends_on_checker():
    """Test that verifier is independent and doesn't read checker output."""
    # This test documents the design principle: verifier uses only raw data
    expenses = create_test_expenses()
    policy = create_test_policy()

    # Verifier should work with no checker output present
    output = verify_compliance(expenses, policy)

    assert len(output.verdicts) == len(expenses)
    assert output.stage == "verifier"
