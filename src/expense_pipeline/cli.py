"""Command-line interface for the expense verification pipeline."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
import uuid

from .ingestion import ingest_expenses
from .policy_parser import parse_policy_manual
from .checker import check_compliance, resolve_provider
from .verifier import verify_compliance
from .approver import approve_expenses
from .snowflake_loader import load_to_snowflake
from .audit import (
    create_audit_file,
    log_stage_completion,
    log_gate_check,
    log_decision,
    log_disagreement,
    log_snowflake_load,
    log_run_summary,
)
from .decision_builder import build_final_decision, expense_snapshot
from .decision_store import DecisionStore
from .decision_view import decision_log_path
from .openai_checker import CheckerOutputError
from .schemas import ApprovedExpense, CheckerOutput, PolicyRules, RunResult


def _load_dotenv() -> None:
    """Load .env into os.environ without overwriting existing variables."""
    candidates = (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env")
    env_path = next((path for path in candidates if path.is_file()), None)
    if env_path is None:
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value.strip())


def main():
    """Main CLI entry point."""
    _load_dotenv()
    parser = argparse.ArgumentParser(
        description="Expense verification pipeline against company spending policy"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run the verification pipeline")
    run_parser.add_argument("spreadsheet", help="Path to expense spreadsheet (XLSX)")
    run_parser.add_argument("policy", help="Path to policy manual (DOCX)")
    run_parser.add_argument(
        "--load-to-snowflake",
        action="store_true",
        help="Load results to Snowflake (requires credentials)",
    )
    run_parser.add_argument(
        "--policy-yaml",
        default="src/expense_pipeline/data/policy_rules.yaml",
        help="Path to save parsed policy rules YAML",
    )
    run_parser.add_argument(
        "--audit-dir",
        default="audit",
        help="Directory for audit JSON files",
    )
    run_parser.add_argument(
        "--reports-dir",
        default="reports",
        help="Directory for report CSV files",
    )
    run_parser.add_argument(
        "--model",
        default=None,
        help="Checker model id. Overrides ANTHROPIC_MODEL or OPENAI_MODEL.",
    )
    run_parser.add_argument(
        "--checker-provider",
        default=None,
        choices=["anthropic", "openai"],
        help="Checker provider. Default is anthropic. OpenAI mode records label probabilities only.",
    )
    run_parser.set_defaults(func=run_pipeline)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def run_pipeline(args):
    """Run the full verification pipeline."""
    run_id = str(uuid.uuid4())[:8]
    print(f"Starting expense verification run: {run_id}")

    audit_file = create_audit_file(args.audit_dir, run_id)
    report_dir = Path(args.reports_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    provider = resolve_provider(args.checker_provider)
    store = DecisionStore(decision_log_path(args.audit_dir))
    store.start_run(run_id, args.spreadsheet, args.policy, provider)

    try:
        # Stage 1: Ingest expenses
        print("Stage 1: Ingesting expenses...")
        expense_sheet = ingest_expenses(args.spreadsheet)
        log_stage_completion(
            audit_file,
            "ingestion",
            "success",
            {"rows": len(expense_sheet.expenses)},
        )
        for expense in expense_sheet.expenses:
            store.append_event(
                run_id,
                expense.report_id,
                "ingestion",
                "input_captured",
                expense_snapshot(expense),
            )
        print(f"  [OK] Loaded {len(expense_sheet.expenses)} expenses")

        # Stage 2: Parse policy
        print("Stage 2: Parsing policy manual...")
        policy_rules = parse_policy_manual(args.policy, args.policy_yaml)
        log_stage_completion(
            audit_file,
            "policy_parser",
            "success",
            {"categories": len(policy_rules.rules)},
        )
        store.append_event(
            run_id,
            None,
            "policy_parser",
            "policy_snapshot_retained",
            {
                "source_file": policy_rules.source_file,
                "categories": list(policy_rules.rules.keys()),
            },
        )
        print(f"  [OK] Parsed {len(policy_rules.rules)} policy categories")

        # Stage 3: LLM-assisted checking
        print(f"Stage 3: Running compliance checks ({provider})...")
        try:
            checker_output = check_compliance(
                expense_sheet.expenses,
                policy_rules,
                model=args.model,
                provider=provider,
            )
        except CheckerOutputError as exc:
            _persist_checker_output(store, run_id, exc.partial)
            raise
        _persist_checker_output(store, run_id, checker_output)
        log_stage_completion(
            audit_file,
            "checker",
            "success",
            {
                "verdicts": len(checker_output.verdicts),
                "flagged": sum(1 for v in checker_output.verdicts if v.verdict == "flagged"),
                "provider": checker_output.provider,
                "model": checker_output.model,
                "prompt_version": checker_output.prompt_version,
            },
        )
        print(
            f"  [OK] Generated {len(checker_output.verdicts)} verdicts "
            f"({checker_output.provider}/{checker_output.model})"
        )

        # Stage 4: Independent verification
        print("Stage 4: Running independent verification...")
        verifier_output = verify_compliance(expense_sheet.expenses, policy_rules)
        log_stage_completion(
            audit_file,
            "verifier",
            "success",
            {
                "verdicts": len(verifier_output.verdicts),
                "flagged": sum(1 for v in verifier_output.verdicts if v.verdict == "flagged"),
            },
        )
        for verdict in verifier_output.verdicts:
            store.append_event(
                run_id,
                verdict.report_id,
                "verifier",
                "checks_completed",
                {
                    "verdict": verdict.verdict,
                    "reasons": list(verdict.reasons),
                    "rule_citations": list(verdict.rule_citations),
                    "checks": list(verdict.checks or []),
                },
            )
        print(f"  [OK] Verified {len(verifier_output.verdicts)} verdicts")

        # Stage 5: Approval and finalization
        print("Stage 5: Finalizing approvals...")
        approved_expenses, disagreements = approve_expenses(
            expense_sheet.expenses,
            checker_output.verdicts,
            verifier_output.verdicts,
        )

        for disagreement in disagreements:
            store.append_event(
                run_id,
                disagreement.report_id,
                "approver",
                "disagreement",
                {
                    "checker_verdict": disagreement.checker_verdict,
                    "verifier_verdict": disagreement.verifier_verdict,
                    "match": disagreement.match,
                },
            )

        records = _decision_records(
            run_id,
            policy_rules,
            checker_output,
            approved_expenses,
            disagreements,
        )
        # Mandatory decision writes happen before reports are treated as final
        # and before any Snowflake load.
        finalize_and_maybe_load(
            store,
            records,
            load=lambda: load_to_snowflake(run_id, approved_expenses),
            do_load=False,
        )

        for approved_exp in approved_expenses:
            verifier = approved_exp.verifier_verdict
            citations = list(verifier.rule_citations or [])
            log_decision(
                audit_file,
                approved_exp.expense.report_id,
                approved_exp.final_status,
                reasoning_path=[
                    f"checker:{approved_exp.checker_verdict.verdict}",
                    f"verifier:{verifier.verdict}",
                    *(verifier.reasons or []),
                ],
                policy_matched=citations[0] if citations else None,
                confidence=None,
                rule_citations=citations,
                reconciliation_rule=approved_exp.reconciliation_rule,
            )

        for disagreement in disagreements:
            log_disagreement(
                audit_file,
                disagreement.report_id,
                disagreement.checker_verdict,
                disagreement.verifier_verdict,
            )

        log_stage_completion(
            audit_file,
            "approver",
            "success",
            {
                "approved": sum(1 for e in approved_expenses if e.final_status == "approved"),
                "flagged": sum(1 for e in approved_expenses if e.final_status == "flagged"),
                "needs_review": sum(
                    1 for e in approved_expenses if e.final_status == "needs_human_review"
                ),
                "disagreements": len(disagreements),
            },
        )
        print(f"  [OK] Approved {sum(1 for e in approved_expenses if e.final_status == 'approved')} expenses")
        print(f"  [OK] Flagged {sum(1 for e in approved_expenses if e.final_status == 'flagged')} expenses")
        print(
            f"  [OK] {sum(1 for e in approved_expenses if e.final_status == 'needs_human_review')} need human review"
        )

        # Create run result
        run_result = RunResult(
            run_id=run_id,
            run_timestamp=datetime.utcnow(),
            expenses=expense_sheet.expenses,
            approved_expenses=approved_expenses,
            approved_count=sum(1 for e in approved_expenses if e.final_status == "approved"),
            flagged_count=sum(1 for e in approved_expenses if e.final_status == "flagged"),
            needs_review_count=sum(
                1 for e in approved_expenses if e.final_status == "needs_human_review"
            ),
            disagreements=disagreements,
        )

        # Stage 6: Optional Snowflake load
        if args.load_to_snowflake:
            print("Stage 6: Loading to Snowflake...")
            store.append_event(
                run_id,
                None,
                "loader",
                "snowflake_load_starting",
                {"rows": len(approved_expenses)},
            )
            try:
                rows_loaded, verified_count = load_to_snowflake(run_id, approved_expenses)
                log_snowflake_load(
                    audit_file,
                    rows_loaded,
                    verified_count,
                    rows_loaded == verified_count,
                )
                store.append_event(
                    run_id,
                    None,
                    "loader",
                    "snowflake_load_finished",
                    {"rows_loaded": rows_loaded, "verified_count": verified_count},
                )
                print(f"  [OK] Loaded {rows_loaded} rows to Snowflake (verified: {verified_count})")
            except Exception as e:
                log_snowflake_load(audit_file, 0, 0, False)
                store.append_event(
                    run_id,
                    None,
                    "loader",
                    "snowflake_load_failed",
                    {"error": str(e)},
                )
                print(f"  [ERROR] Snowflake load failed: {e}", file=sys.stderr)
                raise
        else:
            print("Stage 6: Skipped (use --load-to-snowflake to enable)")

        # Write report CSVs
        print("Writing reports...")
        _write_reports(report_dir, run_result)

        log_run_summary(audit_file, run_result)
        store.complete_run(run_id)

        print(f"\n[OK] Pipeline complete: {run_id}")
        print(f"  Audit trail: {audit_file}")
        print(f"  Decision log: {store.path}")
        print(f"  Reports: {report_dir}")

    except Exception as e:
        print(f"\n[ERROR] Pipeline failed: {e}", file=sys.stderr)
        raise


def _write_reports(report_dir: Path, run_result: RunResult) -> None:
    """Write CSV report files."""
    import csv

    # Verdicts report
    verdicts_file = report_dir / f"{run_result.run_id}_verdicts.csv"
    with open(verdicts_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "report_id",
                "employee",
                "department",
                "category",
                "amount",
                "receipt",
                "checker_verdict",
                "verifier_verdict",
                "final_status",
                "checker_reasons",
                "verifier_reasons",
                "reconciliation_rule",
            ]
        )
        for approved_exp in run_result.approved_expenses:
            writer.writerow(
                [
                    approved_exp.expense.report_id,
                    approved_exp.expense.employee,
                    approved_exp.expense.department,
                    approved_exp.expense.category,
                    approved_exp.expense.amount,
                    "yes" if approved_exp.expense.receipt_attached else "no",
                    approved_exp.checker_verdict.verdict,
                    approved_exp.verifier_verdict.verdict,
                    approved_exp.final_status,
                    "; ".join(approved_exp.checker_verdict.reasons),
                    "; ".join(approved_exp.verifier_verdict.reasons),
                    approved_exp.reconciliation_rule,
                ]
            )

    # Summary report
    summary_file = report_dir / f"{run_result.run_id}_summary.csv"
    with open(summary_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["metric", "value"]
        )
        writer.writerow(["run_id", run_result.run_id])
        writer.writerow(["total_expenses", len(run_result.expenses)])
        writer.writerow(["approved", run_result.approved_count])
        writer.writerow(["flagged", run_result.flagged_count])
        writer.writerow(["needs_review", run_result.needs_review_count])
        writer.writerow(["disagreements", len(run_result.disagreements)])


def finalize_and_maybe_load(store: DecisionStore, records: list[dict], *, load, do_load: bool):
    """Write every final decision before any downstream load. A failed write stops the load."""
    for record in records:
        store.finalize_decision(record)
    if do_load:
        return load()
    return None


def _decision_records(
    run_id: str,
    policy_rules: PolicyRules,
    checker_output: CheckerOutput,
    approved_expenses: list[ApprovedExpense],
    disagreements,
) -> list[dict]:
    assessments = {item["report_id"]: item for item in checker_output.assessments}
    disagreement_ids = {item.report_id for item in disagreements}
    records = []
    for approved in approved_expenses:
        expense = approved.expense
        assessment = assessments.get(expense.report_id)
        if assessment is None:
            raise ValueError(f"Missing AI assessment for {expense.report_id}")
        disagreement = None
        if expense.report_id in disagreement_ids:
            disagreement = {
                "checker_verdict": approved.checker_verdict.verdict,
                "verifier_verdict": approved.verifier_verdict.verdict,
                "checker_reasons": list(approved.checker_verdict.reasons),
                "verifier_reasons": list(approved.verifier_verdict.reasons),
                "reason_sets_equal": set(approved.checker_verdict.reasons)
                == set(approved.verifier_verdict.reasons),
                "match": False,
            }
        records.append(
            build_final_decision(
                run_id=run_id,
                expense=expense,
                policy_rules=policy_rules,
                assessment=assessment,
                verifier_verdict=approved.verifier_verdict,
                final_status=approved.final_status,
                reconciliation_rule=approved.reconciliation_rule,
                disagreement=disagreement,
                errors=[
                    error
                    for error in checker_output.errors
                    if error.get("report_id") in {None, expense.report_id}
                ],
                retries=[
                    retry
                    for retry in checker_output.retries
                    if retry.get("report_id") in {None, expense.report_id}
                    or "report_id" not in retry
                ],
            )
        )
    return records


def _persist_checker_output(store: DecisionStore, run_id: str, output: CheckerOutput) -> None:
    for retry in output.retries:
        store.append_event(
            run_id,
            retry.get("report_id"),
            "checker",
            "retry",
            retry,
        )
    for error in output.errors:
        store.append_event(
            run_id,
            error.get("report_id"),
            "checker",
            "error",
            error,
        )
    for assessment in output.assessments:
        store.append_event(
            run_id,
            assessment.get("report_id"),
            "checker",
            "ai_stated_output",
            {
                "kind": "model_stated_output",
                "verdict": assessment.get("verdict"),
                "stated_justification": assessment.get("stated_justification"),
                "citations": assessment.get("citations"),
                "citations_complete": assessment.get("citations_complete"),
                "provider": assessment.get("provider"),
                "model": assessment.get("model"),
                "prompt_version": assessment.get("prompt_version"),
                "output_status": assessment.get("output_status"),
                "label_probability": assessment.get("label_probability"),
                "probability_unavailable_reason": assessment.get("probability_unavailable_reason"),
                "note": assessment.get("note"),
            },
        )


if __name__ == "__main__":
    main()
