"""Stage 5: Approval and finalization."""

from .schemas import (
    Expense,
    ExpenseVerdict,
    ApprovedExpense,
    VerdictComparison,
)
from .gates import gate_approver, GateFailure

# Exact reconciliation rules. Label probabilities are not an input.
RECONCILE_BOTH_APPROVED = "approve_when_checker_and_verifier_both_approved"
RECONCILE_BOTH_FLAGGED = "flag_when_checker_and_verifier_both_flagged_with_equal_reasons"
RECONCILE_FLAG_REASON_MISMATCH = "needs_human_review_when_both_flagged_but_reasons_differ"
RECONCILE_VERDICT_MISMATCH = "needs_human_review_when_checker_and_verifier_verdicts_differ"

RECONCILIATION_EXPLANATIONS = {
    RECONCILE_BOTH_APPROVED: (
        "Both the checker and the verifier returned approved, so the outcome is approved."
    ),
    RECONCILE_BOTH_FLAGGED: (
        "Both the checker and the verifier returned flagged and their reason lists are equal, "
        "so the outcome is flagged."
    ),
    RECONCILE_FLAG_REASON_MISMATCH: (
        "Both the checker and the verifier returned flagged, but their reason lists differ, "
        "so the outcome is needs human review."
    ),
    RECONCILE_VERDICT_MISMATCH: (
        "The checker and the verifier returned different verdicts, so the outcome is needs human review."
    ),
}


def explain_reconciliation(rule: str) -> str:
    """Return the registered meaning of a reconciliation rule id."""
    return RECONCILIATION_EXPLANATIONS.get(
        rule,
        f"The recorded rule id is {rule}. No additional explanation is registered for that id.",
    )


def approve_expenses(
    expenses: list[Expense],
    checker_verdicts: list[ExpenseVerdict],
    verifier_verdicts: list[ExpenseVerdict],
) -> tuple[list[ApprovedExpense], list[VerdictComparison]]:
    """
    Split expenses into approved/flagged/needs_human_review based on checker+verifier agreement.

    - Approved: both checker and verifier say "approved"
    - Flagged: both checker and verifier say "flagged" and agree on reasons
    - Needs human review: disagreement between checker and verifier
    """
    # Index verdicts by report_id for easy lookup
    checker_by_id = {v.report_id: v for v in checker_verdicts}
    verifier_by_id = {v.report_id: v for v in verifier_verdicts}

    approved_expenses = []
    disagreements = []

    for expense in expenses:
        checker_verdict = checker_by_id.get(expense.report_id)
        verifier_verdict = verifier_by_id.get(expense.report_id)

        if not checker_verdict or not verifier_verdict:
            raise ValueError(f"Missing verdict for {expense.report_id}")

        if checker_verdict.verdict == verifier_verdict.verdict == "approved":
            final_status = "approved"
            reconciliation_rule = RECONCILE_BOTH_APPROVED
        elif checker_verdict.verdict == verifier_verdict.verdict == "flagged":
            checker_reasons_set = set(checker_verdict.reasons)
            verifier_reasons_set = set(verifier_verdict.reasons)
            if checker_reasons_set == verifier_reasons_set:
                final_status = "flagged"
                reconciliation_rule = RECONCILE_BOTH_FLAGGED
            else:
                final_status = "needs_human_review"
                reconciliation_rule = RECONCILE_FLAG_REASON_MISMATCH
                disagreements.append(
                    VerdictComparison(
                        report_id=expense.report_id,
                        checker_verdict=checker_verdict.verdict,
                        verifier_verdict=verifier_verdict.verdict,
                        match=False,
                    )
                )
        else:
            final_status = "needs_human_review"
            reconciliation_rule = RECONCILE_VERDICT_MISMATCH
            disagreements.append(
                VerdictComparison(
                    report_id=expense.report_id,
                    checker_verdict=checker_verdict.verdict,
                    verifier_verdict=verifier_verdict.verdict,
                    match=False,
                )
            )

        approved_expenses.append(
            ApprovedExpense(
                expense=expense,
                checker_verdict=checker_verdict,
                verifier_verdict=verifier_verdict,
                final_status=final_status,
                reconciliation_rule=reconciliation_rule,
            )
        )

    # Gate: confirm all rows have been classified
    try:
        gate_approver(approved_expenses, len(expenses))
    except GateFailure as e:
        raise ValueError(f"Approver gate failed: {e}")

    return approved_expenses, disagreements
