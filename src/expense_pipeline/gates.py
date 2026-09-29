"""Gate checking functions for the pipeline."""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

from .schemas import (
    ExpenseSheet,
    PolicyRules,
    CheckerOutput,
    VerifierOutput,
    ApprovedExpense,
)


class GateFailure(Exception):
    """Raised when a gate check fails."""

    def __init__(self, stage: str, reason: str, detail: Optional[dict] = None):
        self.stage = stage
        self.reason = reason
        self.detail = detail or {}
        super().__init__(f"Gate failure at {stage}: {reason}")


def gate_ingestion(source_file: str, expected_row_count: int) -> ExpenseSheet:
    """
    Gate for Stage 1: re-opens and verifies the ingested file.

    Never trusts the ingestion output's row count alone — actually re-reads
    the file and confirms counts match.
    """
    if not Path(source_file).exists():
        raise GateFailure("ingestion", f"Source file not found: {source_file}")

    try:
        import openpyxl

        wb = openpyxl.load_workbook(source_file, data_only=True)
        ws = wb["expenses"]
        actual_row_count = ws.max_row - 1  # Exclude header
    except Exception as e:
        raise GateFailure("ingestion", f"Failed to verify file: {str(e)}")

    if actual_row_count != expected_row_count:
        raise GateFailure(
            "ingestion",
            f"Row count mismatch",
            {"expected": expected_row_count, "actual": actual_row_count},
        )

    return None


def gate_policy_parser(policy_rules: PolicyRules, expense_categories: set[str]) -> None:
    """Gate for Stage 2: confirms every expense category has a policy rule."""
    missing_categories = expense_categories - set(policy_rules.rules.keys())
    if missing_categories:
        raise GateFailure(
            "policy_parser",
            f"Categories missing policy rules: {missing_categories}",
            {"missing": list(missing_categories)},
        )


def gate_checker(
    input_expenses: list[Any], checker_output: CheckerOutput
) -> None:
    """
    Gate for Stage 3: confirms row-count parity and every row has a verdict.

    Never trusts the checker's status flag — verifies the actual artifacts.
    """
    if len(input_expenses) != len(checker_output.verdicts):
        raise GateFailure(
            "checker",
            "Row count mismatch between input and checker output",
            {
                "input_count": len(input_expenses),
                "output_count": len(checker_output.verdicts),
            },
        )

    missing_verdicts = []
    for expense in input_expenses:
        if not any(v.report_id == expense.report_id for v in checker_output.verdicts):
            missing_verdicts.append(expense.report_id)

    if missing_verdicts:
        raise GateFailure(
            "checker",
            f"Missing verdicts for expenses: {missing_verdicts}",
            {"missing": missing_verdicts},
        )


def gate_verifier(
    input_expenses: list[Any], verifier_output: VerifierOutput
) -> None:
    """
    Gate for Stage 4: confirms row-count parity and every row has a verdict.

    Pure code verification is independent; verifies the verifier also produced
    complete output.
    """
    if len(input_expenses) != len(verifier_output.verdicts):
        raise GateFailure(
            "verifier",
            "Row count mismatch between input and verifier output",
            {
                "input_count": len(input_expenses),
                "output_count": len(verifier_output.verdicts),
            },
        )

    missing_verdicts = []
    for expense in input_expenses:
        if not any(v.report_id == expense.report_id for v in verifier_output.verdicts):
            missing_verdicts.append(expense.report_id)

    if missing_verdicts:
        raise GateFailure(
            "verifier",
            f"Missing verdicts for expenses: {missing_verdicts}",
            {"missing": missing_verdicts},
        )


def gate_approver(approved_expenses: list[ApprovedExpense], row_count: int) -> None:
    """
    Gate for Stage 5: confirms all rows have been classified.

    Verifies that every input expense has been assigned to approved/flagged/needs_review.
    """
    total_approved = sum(
        1 for exp in approved_expenses if exp.final_status == "approved"
    )
    total_flagged = sum(1 for exp in approved_expenses if exp.final_status == "flagged")
    total_needs_review = sum(
        1 for exp in approved_expenses if exp.final_status == "needs_human_review"
    )

    total = total_approved + total_flagged + total_needs_review
    if total != row_count:
        raise GateFailure(
            "approver",
            f"Row count mismatch in approver output",
            {
                "expected": row_count,
                "actual": total,
                "approved": total_approved,
                "flagged": total_flagged,
                "needs_review": total_needs_review,
            },
        )


def gate_snowflake_load_verified(
    snowflake_conn: Any, run_id: str, expected_row_count: int
) -> None:
    """
    Gate for Stage 6: independently verifies the Snowflake load.

    Never trusts the loader's return value. Independently queries Snowflake
    to confirm row count matches expected.
    """
    try:
        cursor = snowflake_conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM EXPENSE_VERDICTS WHERE run_id = %s", (run_id,)
        )
        actual_count = cursor.fetchone()[0]
    except Exception as e:
        raise GateFailure(
            "snowflake_load",
            f"Failed to verify Snowflake load: {str(e)}",
        )

    if actual_count != expected_row_count:
        raise GateFailure(
            "snowflake_load",
            f"Row count mismatch in Snowflake",
            {"expected": expected_row_count, "actual": actual_count},
        )


def log_gate_failure(audit_file: Path, failure: GateFailure) -> None:
    """Log a gate failure to the audit JSON file. Failure blocks the caller."""
    from .audit import AuditWriteError

    audit_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "gate_failure",
        "stage": failure.stage,
        "reason": failure.reason,
        "detail": failure.detail,
    }

    try:
        if audit_file.exists():
            with open(audit_file, "r", encoding="utf-8") as handle:
                audit_log = json.load(handle)
        else:
            audit_log = []

        audit_log.append(audit_entry)

        with open(audit_file, "w", encoding="utf-8") as handle:
            json.dump(audit_log, handle, indent=2)
    except Exception as exc:
        raise AuditWriteError(f"Failed to log gate failure: {exc}") from exc
