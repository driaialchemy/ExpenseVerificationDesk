"""Assemble observed decision records from pipeline artifacts."""

from typing import Optional

from .approver import explain_reconciliation
from .decision_store import DECISION_RECORD_VERSION
from .policy_parser import normalize_category
from .schemas import Expense, ExpenseVerdict, PolicyRules
from .verifier import daily_group_key


def known_clause_ids(policy_rules: PolicyRules) -> set[str]:
    clause_ids: set[str] = set()
    for name, rule in policy_rules.rules.items():
        category = normalize_category(name)
        clause_ids.add(f"{category}.daily_limit")
        clause_ids.add(f"{category}.receipt_required_above")
        if rule.requires_manager_approval_above is not None:
            clause_ids.add(f"{category}.requires_manager_approval_above")
    return clause_ids


def applicable_clause_ids(expense: Expense, policy_rules: PolicyRules) -> list[str]:
    """Clauses that apply to this expense's category. Other categories are not included."""
    category = normalize_category(expense.category)
    rule = next(
        (
            candidate
            for name, candidate in policy_rules.rules.items()
            if normalize_category(name) == category
        ),
        None,
    )
    if rule is None:
        return []
    clause_ids = [
        f"{category}.daily_limit",
        f"{category}.receipt_required_above",
    ]
    if rule.requires_manager_approval_above is not None:
        clause_ids.append(f"{category}.requires_manager_approval_above")
    return clause_ids


def citation_coverage(citations: list, applicable: list[str]) -> dict:
    """Compare the model's citations with the clauses that apply to this expense.

    Missing and unsupported citations are reported. Nothing is added to the model's list.
    """
    stated: list[str] = []
    unsupported: list[str] = []
    for item in citations or []:
        if isinstance(item, str):
            stated.append(item)
        else:
            unsupported.append(str(item))
    applicable_set = set(applicable)
    unsupported.extend(item for item in stated if item not in applicable_set)
    missing = [clause for clause in applicable if clause not in set(stated)]
    complete = bool(applicable) and not missing and not unsupported
    return {
        "applicable": list(applicable),
        "stated": stated,
        "missing": missing,
        "unsupported": unsupported,
        "complete": complete,
    }


def model_input_facts(expense: Expense, expenses: list[Expense]) -> dict:
    """Input rows that share this expense's daily group. Verdicts are not included."""
    key = daily_group_key(expense)
    rows = [expense_snapshot(item) for item in expenses if daily_group_key(item) == key]
    return {
        "grouping": "same_employee_calendar_day_category_currency",
        "employee": key[0],
        "calendar_day": key[1],
        "category": key[2],
        "currency": key[3],
        "rows": rows,
        "includes_verdicts": False,
    }


def render_daily_context(expenses: list[Expense]) -> str:
    """Text of input facts for daily groups. This is not a verifier verdict."""
    groups: dict[tuple, list[Expense]] = {}
    for expense in expenses:
        groups.setdefault(daily_group_key(expense), []).append(expense)
    lines = ["Daily context (input rows only; no checker or verifier verdicts):"]
    for key, members in groups.items():
        employee, day, category, currency = key
        lines.append(
            f"Group employee={employee} day={day} category={category} currency={currency}"
        )
        for member in members:
            lines.append(
                f"- {member.report_id} amount {member.amount} {member.currency} "
                f"receipt_attached={member.receipt_attached} category={member.category} "
                f"date={member.date}"
            )
    return "\n".join(lines)


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
        "reconciliation_explanation": explain_reconciliation(reconciliation_rule),
        "errors": list(errors),
        "retries": list(retries),
        "human_review": None,
        "overrides": [],
    }
