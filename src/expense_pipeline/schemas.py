"""Data models and schemas for the expense verification pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Expense:
    """Single expense row from the spreadsheet."""

    report_id: str
    employee: str
    department: str
    date: str
    category: str
    amount: float
    currency: str
    receipt_attached: bool
    notes: Optional[str] = None
    source_row: Optional[int] = None


@dataclass
class ExpenseSheet:
    """Parsed expense spreadsheet."""

    expenses: list[Expense]
    source_file: str
    row_count: int


@dataclass
class PolicyRule:
    """Policy rule for a category."""

    category: str
    daily_limit: float
    receipt_required_above: float
    requires_manager_approval_above: Optional[float] = None


@dataclass
class PolicyRules:
    """All policy rules for the company."""

    rules: dict[str, PolicyRule]
    source_file: str


@dataclass
class ExpenseVerdict:
    """Verdict for a single expense."""

    report_id: str
    verdict: str  # "approved" | "flagged"
    reasons: list[str]
    rule_citations: list[str]
    checks: Optional[list] = None


@dataclass
class CheckerOutput:
    """Output from the compliance checker stage."""

    verdicts: list[ExpenseVerdict]
    stage: str = "checker"
    provider: str = "anthropic"
    model: str = ""
    prompt_version: str = ""
    settings: dict = field(default_factory=dict)
    assessments: list = field(default_factory=list)
    retries: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class VerifierOutput:
    """Output from the independent verifier stage."""

    verdicts: list[ExpenseVerdict]
    stage: str = "verifier"


@dataclass
class VerdictComparison:
    """Comparison between checker and verifier verdicts."""

    report_id: str
    checker_verdict: str
    verifier_verdict: str
    match: bool


@dataclass
class ApprovedExpense:
    """Expense with final approval status."""

    expense: Expense
    checker_verdict: ExpenseVerdict
    verifier_verdict: ExpenseVerdict
    final_status: str  # "approved" | "flagged" | "needs_human_review"
    reconciliation_rule: str = ""


@dataclass
class RunResult:
    """Final result of a pipeline run."""

    run_id: str
    run_timestamp: datetime
    expenses: list[Expense]
    approved_expenses: list[ApprovedExpense]
    approved_count: int
    flagged_count: int
    needs_review_count: int
    disagreements: list[VerdictComparison] = field(default_factory=list)


@dataclass
class RunSummary:
    """Summary row for Snowflake RUN_SUMMARY table."""

    run_id: str
    run_timestamp: datetime
    rows_processed: int
    approved_count: int
    flagged_count: int
    needs_review_count: int
