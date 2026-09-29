"""Decision lineage, audit blocking, and dashboard retrieval."""

import ast
import sqlite3
from pathlib import Path

import pytest

from src.expense_pipeline.approver import (
    RECONCILE_BOTH_APPROVED,
    RECONCILE_VERDICT_MISMATCH,
    approve_expenses,
)
from src.expense_pipeline.audit import AuditWriteError, log_decision
from src.expense_pipeline.cli import finalize_and_maybe_load
from src.expense_pipeline.decision_builder import build_final_decision
from src.expense_pipeline.decision_store import DecisionStore
from src.expense_pipeline.decision_view import PROBABILITY_EXPLANATION, load_decision_process
from src.expense_pipeline.schemas import Expense, ExpenseVerdict, PolicyRule, PolicyRules


def _expense(report_id="EXP-0001", amount=50.0, employee="Alice", day="2024-01-15", currency="USD"):
    return Expense(
        report_id=report_id,
        employee=employee,
        department="Engineering",
        date=day,
        category="meals",
        amount=amount,
        currency=currency,
        receipt_attached=True,
        notes="team lunch",
        source_row=2,
    )


def _policy():
    return PolicyRules(
        rules={
            "meals": PolicyRule(
                category="meals",
                daily_limit=75.0,
                receipt_required_above=30.0,
            )
        },
        source_file="policy.docx",
    )


def _assessment(verdict="approved", probability=None, reason="provider_does_not_return_label_logprobs"):
    return {
        "kind": "model_stated_output",
        "note": "stated_justification is text the model produced.",
        "report_id": "EXP-0001",
        "verdict": verdict,
        "stated_justification": ["Within the meal limit."],
        "citations": ["meals.daily_limit"],
        "citations_complete": True,
        "provider": "openai" if probability is not None else "anthropic",
        "model": "gpt-test" if probability is not None else "claude-sonnet-5",
        "prompt_version": "test-v1",
        "settings": {"temperature": 0},
        "output_status": "ok",
        "label_logprob": None if probability is None else -0.693147,
        "label_probability": probability,
        "probability_unavailable_reason": None if probability is not None else reason,
        "alternatives": [],
    }


def _verdict(report_id="EXP-0001", verdict="approved", reasons=None):
    return ExpenseVerdict(
        report_id=report_id,
        verdict=verdict,
        reasons=reasons or [],
        rule_citations=["meals.daily_limit", "meals.receipt_required_above"],
        checks=[
            {
                "clause": "meals.daily_limit",
                "threshold": 75.0,
                "observed_value": 50.0,
                "result": "pass",
            }
        ],
    )


def _record(probability=None, final_status="approved", rule=RECONCILE_BOTH_APPROVED):
    expense = _expense()
    assessment = _assessment(verdict="approved", probability=probability)
    assessment["report_id"] = expense.report_id
    return build_final_decision(
        run_id="run1",
        expense=expense,
        policy_rules=_policy(),
        assessment=assessment,
        verifier_verdict=_verdict(),
        final_status=final_status,
        reconciliation_rule=rule,
        disagreement=None,
        errors=[],
        retries=[{"component": "checker", "action": "retry_over_ipv4"}],
    )


def test_decision_lineage_and_reopen(tmp_path):
    store = DecisionStore(tmp_path / "decision_log.sqlite")
    store.start_run("run1", "expenses.xlsx", "policy.docx", "anthropic")
    store.append_event("run1", "EXP-0001", "ingestion", "input_captured", {"source_row": 2})
    stored = store.finalize_decision(_record())

    reopened = DecisionStore(tmp_path / "decision_log.sqlite")
    latest = reopened.get_latest("run1", "EXP-0001")
    assert latest["decision_id"] == stored["decision_id"]
    assert latest["run_id"] == "run1"
    assert latest["expense_id"] == "EXP-0001"
    assert latest["version"] == 1
    assert latest["source_row"] == 2
    assert latest["input_facts"]["amount"] == 50.0
    assert latest["synthetic_input_snapshot"]["notes"] == "team lunch"
    assert latest["policy_snapshot"]["clauses"][0]["id"] == "meals.daily_limit"
    assert latest["applied_clauses"][0]["clause"] == "meals.daily_limit"
    assert latest["ai_assessment"]["stated_justification"] == ["Within the meal limit."]
    assert "internal_reasoning" not in latest["ai_assessment"]
    assert latest["verifier"]["observed_values"]["meals.daily_limit"] == 50.0
    assert latest["final_outcome"] == "approved"
    assert latest["reconciliation_rule"] == RECONCILE_BOTH_APPROVED
    assert latest["human_review"] is None
    assert latest["retries"][0]["action"] == "retry_over_ipv4"
    events = reopened.list_events("run1", "EXP-0001")
    assert [event["event_type"] for event in events] == ["input_captured", "decision_recorded"]


def test_disagreement_is_stored_and_is_not_ground_truth():
    expense = _expense(amount=100.0)
    checker = ExpenseVerdict("EXP-0001", "approved", ["Looks fine"], ["meals.daily_limit"])
    verifier = ExpenseVerdict(
        "EXP-0001",
        "flagged",
        ["Exceeds daily limit of $75.0"],
        ["meals.daily_limit"],
    )
    approved, disagreements = approve_expenses([expense], [checker], [verifier])

    assert approved[0].final_status == "needs_human_review"
    assert approved[0].reconciliation_rule == RECONCILE_VERDICT_MISMATCH
    assert disagreements[0].match is False
    # Agreement between the two components is not treated as proof the expense is correct.
    assert approved[0].final_status != "approved"


def test_probability_does_not_change_approval():
    expense = _expense()
    checker = ExpenseVerdict("EXP-0001", "approved", [], ["meals.daily_limit"])
    verifier = ExpenseVerdict("EXP-0001", "approved", [], ["meals.daily_limit"])
    low, _ = approve_expenses([expense], [checker], [verifier])
    high, _ = approve_expenses([expense], [checker], [verifier])
    assert low[0].final_status == high[0].final_status == "approved"

    record_low = _record(probability=0.01)
    record_high = _record(probability=0.99)
    assert record_low["final_outcome"] == record_high["final_outcome"] == "approved"
    assert record_low["ai_assessment"]["label_probability"] == 0.01
    assert record_high["verifier"].get("label_probability") is None


def test_versions_and_events_survive_a_later_human_review(tmp_path):
    store = DecisionStore(tmp_path / "decision_log.sqlite")
    store.start_run("run1", "expenses.xlsx", "policy.docx", "anthropic")
    store.append_event(
        "run1",
        "EXP-0001",
        "checker",
        "ai_stated_output",
        {"stated_justification": ["Within the meal limit."]},
    )
    first = store.finalize_decision(_record())
    second = store.record_human_review(
        "run1",
        "EXP-0001",
        reviewer="Riley Chen",
        note="Receipt photo is unreadable; send back.",
        outcome="needs_human_review",
        reconciliation_rule="human_review_recorded_as_entered",
    )

    assert store.get_version("run1", "EXP-0001", 1)["final_outcome"] == "approved"
    assert store.get_version("run1", "EXP-0001", 1)["decision_id"] == first["decision_id"]
    assert second["version"] == 2
    assert second["final_outcome"] == "needs_human_review"
    assert second["human_review"]["reviewer"] == "Riley Chen"
    assert second["overrides"][0]["prior_decision_id"] == first["decision_id"]
    event_types = [event["event_type"] for event in store.list_events("run1", "EXP-0001")]
    assert event_types[:2] == ["ai_stated_output", "decision_recorded"]
    assert "human_review" in event_types
    assert event_types.count("decision_recorded") == 2


def test_failed_finalize_blocks_downstream_load(tmp_path):
    store = DecisionStore(tmp_path / "decision_log.sqlite")
    loaded = []

    def load():
        loaded.append("snowflake")
        return (1, 1)

    incomplete = _record()
    incomplete["reconciliation_rule"] = ""
    with pytest.raises(AuditWriteError):
        finalize_and_maybe_load(store, [incomplete], load=load, do_load=True)
    assert loaded == []
    assert store.get_latest("run1", "EXP-0001") is None


def test_sqlite_write_failure_raises(tmp_path, monkeypatch):
    store = DecisionStore(tmp_path / "decision_log.sqlite")

    def broken():
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "_connect", broken)
    with pytest.raises(AuditWriteError):
        store.append_event("run1", "EXP-0001", "checker", "retry", {"attempt": 2})


def test_json_audit_failure_is_not_swallowed(tmp_path):
    missing = tmp_path / "missing-dir" / "audit.json"
    with pytest.raises(AuditWriteError):
        log_decision(missing, "EXP-0001", "approved")


def test_secret_is_rejected(tmp_path):
    store = DecisionStore(tmp_path / "decision_log.sqlite")
    record = _record()
    record["errors"] = [{"error": "header sk-ant-api03-SECRETVALUE"}]
    with pytest.raises(AuditWriteError):
        store.finalize_decision(record)


def test_dashboard_retrieval_complete_incomplete_and_legacy(tmp_path):
    audit_dir = tmp_path / "audit"
    store = DecisionStore(audit_dir / "decision_log.sqlite")
    store.start_run("run1", "expenses.xlsx", "policy.docx", "openai")
    store.finalize_decision(_record(probability=0.5))

    complete = load_decision_process("run1", "EXP-0001", audit_dir=audit_dir)
    assert complete["status"] == "complete"
    assert complete["sections"]["outcome"]["final_outcome"] == "approved"
    assert complete["sections"]["evidence"]["source_row"] == 2
    assert complete["sections"]["policy"]["applied_clauses"][0]["clause"] == "meals.daily_limit"
    assert complete["sections"]["ai_assessment"]["stated_justification"] == ["Within the meal limit."]
    probability = complete["sections"]["probability"]
    assert probability["label"] == "Model output probability"
    assert probability["value"] == 0.5
    assert probability["model"] == "gpt-test"
    assert "not the probability that the expense decision is correct" in probability["explanation"]
    assert probability["explanation"] == PROBABILITY_EXPLANATION
    assert complete["sections"]["verifier"]["verdict"] == "approved"
    assert complete["sections"]["reconciliation"]["rule"] == RECONCILE_BOTH_APPROVED
    assert any(event["event_type"] == "decision_recorded" for event in complete["sections"]["timeline"])

    incomplete_record = _record()
    incomplete_record["expense_id"] = "EXP-0002"
    incomplete_record["completeness"] = "incomplete"
    incomplete_record["final_outcome"] = None
    incomplete_record["reconciliation_rule"] = None
    incomplete_record["ai_assessment"]["report_id"] = "EXP-0002"
    store.write_decision(incomplete_record)
    incomplete = load_decision_process("run1", "EXP-0002", audit_dir=audit_dir)
    assert incomplete["status"] == "incomplete"
    assert "not finish" in incomplete["detail"]

    legacy = load_decision_process("run1", "EXP-0099", audit_dir=audit_dir)
    assert legacy["status"] == "legacy"
    assert legacy["record"] is None

    missing_log = load_decision_process("run1", "EXP-0001", audit_dir=tmp_path / "empty")
    assert missing_log["status"] == "legacy"


def test_decision_view_does_not_import_a_model_client():
    source = Path("src/expense_pipeline/decision_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"httpx", "anthropic", "openai", "requests", "urllib", "socket"})


def test_verifier_module_has_no_network_or_llm_imports():
    source = Path("src/expense_pipeline/verifier.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(
        {"httpx", "anthropic", "openai", "requests", "urllib", "socket", "checker"}
    )
