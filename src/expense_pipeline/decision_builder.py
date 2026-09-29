"""Assemble observed decision records from pipeline artifacts."""

from typing import Optional

from .decision_store import DECISION_RECORD_VERSION
from .policy_parser import normalize_category
from .schemas import Expense, ExpenseVerdict, PolicyRules


def known_clause_ids(policy_rules: PolicyRules) -> set[str]:
    clause_ids: set[str] = set()
    for name, rule in policy_rules.rules.items():
        category = normalize_category(name)
        clause_ids.add(f"{category}.daily_limit")
        clause_ids.add(f"{category}.receipt_required_above")
        if rule.requires_manager_approval_above is not None:
            clause_ids.add(f"{category}.requires_manager_approval_above")
    return clause_ids


def citations_are_complete(citations: list[str], clause_ids: set[str]) -> bool:
    """A citation list is complete only when every stated citation is a known clause."""
    if not citations:
        return False
    return all(citation in clause_ids for citation in citations)


def expense_snapshot(expense: Expense) -> dict:
    return {
        "report_id": expense.report_id,
        "employee": expense.employee,
        "department": expense.department,
        "date": expense.date,
        "category": expense.category,
        "amount": expense.amount,
        "currency": expense.currency,
        "receipt_attached": expense.receipt_attached,
        "notes": expense.notes,
        "source_row": expense.source_row,
    }


def policy_snapshot_for(expense: Expense, policy_rules: PolicyRules) -> dict:
    category = normalize_category(expense.category)
    rule = next(
        (
            candidate
            for name, candidate in policy_rules.rules.items()
            if normalize_category(name) == category
        ),
        None,
    )
    clauses = []
    if rule is not None:
        clauses.append(
            {
                "id": f"{category}.daily_limit",
                "threshold": rule.daily_limit,
                "scope": "daily_sum_same_employee_day_category_currency",
            }
        )
        clauses.append(
            {
                "id": f"{category}.receipt_required_above",
                "threshold": rule.receipt_required_above,
                "scope": "per_expense",
            }
        )
        if rule.requires_manager_approval_above is not None:
            clauses.append(
                {
                    "id": f"{category}.requires_manager_approval_above",
                    "threshold": rule.requires_manager_approval_above,
                    "scope": "per_expense",
                }
            )
    return {
        "source_file": policy_rules.source_file,
        "category": expense.category,
        "normalized_category": category,
        "clauses": clauses,
    }


def build_final_decision(
    *,
    run_id: str,
    expense: Expense,
    policy_rules: PolicyRules,
    assessment: dict,
    verifier_verdict: ExpenseVerdict,
    final_status: str,
    reconciliation_rule: str,
    disagreement: Optional[dict],
    errors: list,
    retries: list,
) -> dict:
    checks = list(verifier_verdict.checks or [])
    return {
        "schema_version": DECISION_RECORD_VERSION,
        "run_id": run_id,
        "expense_id": expense.report_id,
        "responsible_component": "approver",
        "completeness": "complete",
        "source_row": expense.source_row,
        "input_facts": expense_snapshot(expense),
        "synthetic_input_snapshot": expense_snapshot(expense),
        "policy_snapshot": policy_snapshot_for(expense, policy_rules),
        "applied_clauses": checks,
        "ai_assessment": assessment,
        "verifier": {
            "verdict": verifier_verdict.verdict,
            "reasons": list(verifier_verdict.reasons),
            "rule_citations": list(verifier_verdict.rule_citations),
            "checks": checks,
            "observed_values": {
                check.get("clause"): check.get("observed_value")
                for check in checks
                if check.get("clause")
            },
        },
        "disagreement": disagreement,
        "final_outcome": final_status,
        "reconciliation_rule": reconciliation_rule,
        "errors": list(errors),
        "retries": list(retries),
        "human_review": None,
        "overrides": [],
    }
