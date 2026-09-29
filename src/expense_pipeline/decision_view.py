"""Read stored decision records for display. This module makes no model calls."""

from pathlib import Path
from typing import Optional

from .decision_store import DECISION_RECORD_VERSION, DecisionStore

PROBABILITY_EXPLANATION = (
    "Model output probability is the exponential of the logprob of the label token "
    "the model actually emitted. It is not the probability that the expense decision is correct."
)

_HEADLINES = {
    "complete": "Stored decision process",
    "incomplete": "Incomplete decision record",
    "legacy": "Legacy record",
}


def decision_log_path(audit_dir: str | Path = "audit") -> Path:
    return Path(audit_dir) / "decision_log.sqlite"


def load_decision_process(
    run_id: str,
    expense_id: str,
    audit_dir: str | Path = "audit",
    store: Optional[DecisionStore] = None,
) -> dict:
    """Return the stored decision view for one expense."""
    if store is None:
        path = decision_log_path(audit_dir)
        if not path.exists():
            return _legacy(
                run_id,
                expense_id,
                "No decision log was found. This run has an export, but the decision process was not captured.",
            )
        store = DecisionStore(path)

    record = store.get_latest(run_id, expense_id)
    events = store.list_events(run_id, expense_id)
    if record is None:
        return _legacy(
            run_id,
            expense_id,
            "This expense has no versioned decision record. Earlier exports may still show a verdict.",
            events,
        )

    status = record.get("completeness") or "incomplete"
    if record.get("schema_version") != DECISION_RECORD_VERSION:
        status = "legacy"
    detail = {
        "complete": "This view is the stored record. It was loaded from the local decision log.",
        "incomplete": "Finalization did not finish for this expense. Do not treat the outcome as final.",
        "legacy": "This record uses an older decision schema. Fields may be missing.",
    }[status]
    return {
        "status": status,
        "headline": _HEADLINES[status],
        "detail": detail,
        "record": record,
        "events": events,
        "sections": _sections(record, events),
    }


def _legacy(run_id: str, expense_id: str, detail: str, events: Optional[list] = None) -> dict:
    return {
        "status": "legacy",
        "headline": _HEADLINES["legacy"],
        "detail": detail,
        "record": None,
        "events": events or [],
        "sections": {
            "outcome": None,
            "evidence": None,
            "policy": None,
            "ai_assessment": None,
            "probability": {
                "label": "Model output probability",
                "value": None,
                "reason": "legacy_record",
                "explanation": PROBABILITY_EXPLANATION,
            },
            "verifier": None,
            "reconciliation": None,
            "timeline": events or [],
        },
        "run_id": run_id,
        "expense_id": expense_id,
    }


def _sections(record: dict, events: list[dict]) -> dict:
    ai = record.get("ai_assessment") or {}
    return {
        "outcome": {
            "final_outcome": record.get("final_outcome"),
            "decision_id": record.get("decision_id"),
            "version": record.get("version"),
            "run_id": record.get("run_id"),
            "expense_id": record.get("expense_id"),
            "updated_at": record.get("updated_at"),
            "responsible_component": record.get("responsible_component"),
        },
        "evidence": {
            "source_row": record.get("source_row"),
            "input_facts": record.get("input_facts"),
            "synthetic_input_snapshot": record.get("synthetic_input_snapshot"),
        },
        "policy": {
            "snapshot": record.get("policy_snapshot"),
            "applied_clauses": record.get("applied_clauses"),
        },
        "ai_assessment": {
            "label": "AI assessment (model-stated output)",
            "note": ai.get("note"),
            "verdict": ai.get("verdict"),
            "stated_justification": ai.get("stated_justification"),
            "citations": ai.get("citations"),
            "citations_complete": ai.get("citations_complete"),
            "provider": ai.get("provider"),
            "model": ai.get("model"),
            "prompt_version": ai.get("prompt_version"),
            "settings": ai.get("settings"),
            "output_status": ai.get("output_status"),
        },
        "probability": {
            "label": "Model output probability",
            "value": ai.get("label_probability"),
            "logprob": ai.get("label_logprob"),
            "reason": ai.get("probability_unavailable_reason"),
            "alternatives": ai.get("alternatives") or [],
            "provider": ai.get("provider"),
            "model": ai.get("model"),
            "explanation": PROBABILITY_EXPLANATION,
        },
        "verifier": record.get("verifier"),
        "reconciliation": {
            "rule": record.get("reconciliation_rule"),
            "disagreement": record.get("disagreement"),
            "final_outcome": record.get("final_outcome"),
            "human_review": record.get("human_review"),
            "overrides": record.get("overrides"),
            "errors": record.get("errors"),
            "retries": record.get("retries"),
        },
        "timeline": [
            {
                "occurred_at": event["occurred_at"],
                "component": event["component"],
                "event_type": event["event_type"],
                "payload": event["payload"],
            }
            for event in events
        ],
    }
