"""Audit trail logging for the pipeline."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .schemas import RunResult


class AuditWriteError(RuntimeError):
    """A mandatory audit write failed. Finalization and downstream loads must stop."""


def create_audit_file(audit_dir: str, run_id: str) -> Path:
    """Create audit directory and return path to audit JSON file."""
    audit_path = Path(audit_dir)
    audit_path.mkdir(parents=True, exist_ok=True)
    return audit_path / f"{run_id}.json"


def log_stage_completion(audit_file: Path, stage: str, status: str, detail: Optional[dict] = None) -> None:
    """Log completion of a pipeline stage."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "stage_completion",
        "stage": stage,
        "status": status,
        "detail": detail or {},
    }
    _append_to_audit(audit_file, entry)


def log_gate_check(audit_file: Path, stage: str, passed: bool, detail: Optional[dict] = None) -> None:
    """Log a gate check result."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "gate_check",
        "stage": stage,
        "passed": passed,
        "detail": detail or {},
    }
    _append_to_audit(audit_file, entry)


def log_decision(
    audit_file: Path,
    report_id: str,
    outcome: str,
    reasoning_path: Optional[list[str]] = None,
    policy_matched: Optional[str] = None,
    confidence: Optional[float] = None,
    rule_citations: Optional[list[str]] = None,
    reconciliation_rule: Optional[str] = None,
) -> None:
    """Log one approve/flag/escalate decision. Lineage shape: governance-logger/docs/decision-lineage-schema.md."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "decision",
        "report_id": report_id,
        "outcome": outcome,
        "reasoning_path": reasoning_path,
        "policy_matched": policy_matched,
        "confidence": confidence,
        "rule_citations": rule_citations or [],
        "reconciliation_rule": reconciliation_rule,
    }
    _append_to_audit(audit_file, entry)


def log_disagreement(audit_file: Path, report_id: str, checker_verdict: str, verifier_verdict: str) -> None:
    """Log a disagreement between checker and verifier."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "verdict_disagreement",
        "report_id": report_id,
        "checker_verdict": checker_verdict,
        "verifier_verdict": verifier_verdict,
    }
    _append_to_audit(audit_file, entry)


def log_snowflake_load(audit_file: Path, row_count: int, verified_count: int, success: bool) -> None:
    """Log Snowflake load result."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "snowflake_load",
        "rows_inserted": row_count,
        "verified_count": verified_count,
        "success": success,
    }
    _append_to_audit(audit_file, entry)


def log_run_summary(audit_file: Path, run_result: RunResult) -> None:
    """Log final run summary."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "run_complete",
        "run_id": run_result.run_id,
        "total_expenses": len(run_result.expenses),
        "approved_count": run_result.approved_count,
        "flagged_count": run_result.flagged_count,
        "needs_review_count": run_result.needs_review_count,
        "disagreements": len(run_result.disagreements),
    }
    _append_to_audit(audit_file, entry)


def _append_to_audit(audit_file: Path, entry: dict) -> None:
    """Append an entry to the audit JSON file. Failure blocks the caller."""
    try:
        if audit_file.exists():
            with open(audit_file, "r", encoding="utf-8") as handle:
                audit_log = json.load(handle)
        else:
            audit_log = []

        audit_log.append(entry)

        with open(audit_file, "w", encoding="utf-8") as handle:
            json.dump(audit_log, handle, indent=2, default=str)
    except Exception as exc:
        raise AuditWriteError(f"Failed to log to audit file: {exc}") from exc
