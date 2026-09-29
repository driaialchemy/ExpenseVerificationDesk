"""Stage 4: Pure-code independent verification (no LLM)."""

from .schemas import Expense, PolicyRules, ExpenseVerdict, VerifierOutput
from .gates import gate_verifier, GateFailure
from .policy_parser import normalize_category

AGGREGATION_RULE = (
    "Sum amounts only when employee, calendar day, category, and currency all match. "
    "Currencies are not converted."
)


def calendar_day(value: str) -> str:
    """Use the leading YYYY-MM-DD when the source date has that shape."""
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


def daily_group_key(expense: Expense) -> tuple[str, str, str, str]:
    return (
        (expense.employee or "").strip(),
        calendar_day(expense.date),
        normalize_category(expense.category),
        (expense.currency or "").strip().upper(),
    )


def verify_compliance(expenses: list[Expense], policy_rules: PolicyRules) -> VerifierOutput:
    """
    Pure code verification of expenses against policy rules.

    Does NOT read Checker output. Re-derives verdicts independently using only
    the raw spreadsheet and raw policy YAML.

    Gate confirms row-count parity and every row has a verdict.
    """
    groups: dict[tuple[str, str, str, str], list[Expense]] = {}
    for expense in expenses:
        groups.setdefault(daily_group_key(expense), []).append(expense)
    totals = {
        key: sum(member.amount for member in members) for key, members in groups.items()
    }

    verdicts = []
    for expense in expenses:
        key = daily_group_key(expense)
        verdicts.append(
            _verify_single_expense(expense, policy_rules, totals[key], groups[key])
        )

    output = VerifierOutput(verdicts=verdicts)

    try:
        gate_verifier(expenses, output)
    except GateFailure as e:
        raise ValueError(f"Verifier gate failed: {e}")

    return output


def _verify_single_expense(
    expense: Expense,
    policy_rules: PolicyRules,
    daily_total: float,
    group_members: list[Expense],
) -> ExpenseVerdict:
    """Verify a single expense against the clauses that apply to its category."""
    reasons = []
    rule_citations = []
    checks = []

    category_lower = normalize_category(expense.category)
    rule = next(
        (
            candidate
            for name, candidate in policy_rules.rules.items()
            if normalize_category(name) == category_lower
        ),
        None,
    )
    if rule is None:
        detail = f"Unknown category: {expense.category}"
        reasons.append(detail)
        checks.append(
            {
                "clause": None,
                "threshold": None,
                "observed_value": None,
                "result": "fail",
                "detail": detail,
            }
        )
        return ExpenseVerdict(
            report_id=expense.report_id,
            verdict="flagged",
            reasons=reasons,
            rule_citations=rule_citations,
            checks=checks,
        )

    member_ids = [member.report_id for member in group_members]
    daily_clause = f"{category_lower}.daily_limit"
    daily_failed = daily_total > rule.daily_limit
    daily_detail = (
        f"Observed {expense.currency} total {daily_total:.2f} for {expense.employee} "
        f"on {calendar_day(expense.date)} in {category_lower} "
        f"across {', '.join(member_ids)}. Threshold {rule.daily_limit:.2f}. {AGGREGATION_RULE}"
    )
    checks.append(
        {
            "clause": daily_clause,
            "threshold": rule.daily_limit,
            "observed_value": daily_total,
            "observed_currency": expense.currency,
            "contributing_report_ids": member_ids,
            "aggregation": "same_employee_calendar_day_category_currency",
            "result": "fail" if daily_failed else "pass",
            "detail": daily_detail,
        }
    )
    rule_citations.append(daily_clause)
    if daily_failed:
        reasons.append(
            f"Exceeds daily limit of ${rule.daily_limit} "
            f"(observed {expense.currency} total ${daily_total:.2f} across {', '.join(member_ids)})"
        )

    receipt_clause = f"{category_lower}.receipt_required_above"
    receipt_failed = (
        expense.amount > rule.receipt_required_above and not expense.receipt_attached
    )
    checks.append(
        {
            "clause": receipt_clause,
            "threshold": rule.receipt_required_above,
            "observed_value": expense.amount,
            "receipt_attached": expense.receipt_attached,
            "aggregation": "per_expense",
            "result": "fail" if receipt_failed else "pass",
            "detail": (
                f"Line amount {expense.amount:.2f} compared with receipt threshold "
                f"{rule.receipt_required_above:.2f}; receipt_attached={expense.receipt_attached}."
            ),
        }
    )
    rule_citations.append(receipt_clause)
    if receipt_failed:
        reasons.append(f"Receipt required for amounts > ${rule.receipt_required_above}")

    if rule.requires_manager_approval_above is not None:
        approval_clause = f"{category_lower}.requires_manager_approval_above"
        approval_failed = expense.amount > rule.requires_manager_approval_above
        checks.append(
            {
                "clause": approval_clause,
                "threshold": rule.requires_manager_approval_above,
                "observed_value": expense.amount,
                "aggregation": "per_expense",
                "result": "fail" if approval_failed else "pass",
                "detail": (
                    f"Line amount {expense.amount:.2f} compared with manager-approval threshold "
                    f"{rule.requires_manager_approval_above:.2f}."
                ),
            }
        )
        rule_citations.append(approval_clause)
        if approval_failed:
            reasons.append(
                "Manager approval required for amounts > "
                f"${rule.requires_manager_approval_above}"
            )

    verdict = "approved" if not reasons else "flagged"
    return ExpenseVerdict(
        report_id=expense.report_id,
        verdict=verdict,
        reasons=reasons,
        rule_citations=rule_citations,
        checks=checks,
    )
