"""Transactional, versioned decision log.

Records what the pipeline observed. AI text is stored as model-stated output.
The log does not claim access to a model's internal reasoning.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .audit import AuditWriteError

AUDIT_SCHEMA_VERSION = 1
DECISION_RECORD_VERSION = 1

_SECRET_RE = re.compile(r"sk-(?:ant|proj)-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{12,}")

_CONTENT_FIELDS = (
    "schema_version",
    "run_id",
    "expense_id",
    "responsible_component",
    "completeness",
    "source_row",
    "input_facts",
    "synthetic_input_snapshot",
    "policy_snapshot",
    "applied_clauses",
    "ai_assessment",
    "verifier",
    "disagreement",
    "final_outcome",
    "reconciliation_rule",
    "reconciliation_explanation",
    "errors",
    "retries",
    "human_review",
    "overrides",
)

_FORBIDDEN_AI_FIELDS = {"internal_reasoning", "chain_of_thought", "hidden_reasoning"}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    source_spreadsheet TEXT,
    source_policy TEXT,
    checker_provider TEXT
);
CREATE TABLE IF NOT EXISTS decision_records (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    expense_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    responsible_component TEXT NOT NULL,
    completeness TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (run_id, expense_id, version)
);
CREATE TABLE IF NOT EXISTS decision_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    expense_id TEXT,
    decision_id TEXT,
    occurred_at TEXT NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _reject_secrets(payload: dict) -> None:
    blob = _dumps(payload)
    if _SECRET_RE.search(blob):
        raise AuditWriteError("Refusing to store a secret in the decision log")


def validate_decision_record(record: dict, *, final: bool) -> None:
    """Reject a decision record that is missing lineage or claims hidden reasoning."""
    missing = [key for key in _CONTENT_FIELDS if key not in record]
    if missing:
        raise AuditWriteError(f"Decision record missing fields: {', '.join(missing)}")
    if record["schema_version"] != DECISION_RECORD_VERSION:
        raise AuditWriteError(
            f"Unsupported decision record version: {record['schema_version']}"
        )
    if record["completeness"] not in {"complete", "incomplete", "legacy"}:
        raise AuditWriteError(f"Unknown completeness: {record['completeness']}")
    if not isinstance(record["applied_clauses"], list):
        raise AuditWriteError("applied_clauses must be a list")
    if not isinstance(record["errors"], list) or not isinstance(record["retries"], list):
        raise AuditWriteError("errors and retries must be lists")
    if not isinstance(record["overrides"], list):
        raise AuditWriteError("overrides must be a list")
    verifier = record.get("verifier") or {}
    if isinstance(verifier, dict) and "label_probability" in verifier:
        raise AuditWriteError("Verifier results cannot carry model label probabilities")

    ai = record.get("ai_assessment")
    if ai is not None:
        if not isinstance(ai, dict):
            raise AuditWriteError("ai_assessment must be an object or null")
        if ai.get("kind") != "model_stated_output":
            raise AuditWriteError("AI assessment must be marked as model-stated output")
        if _FORBIDDEN_AI_FIELDS & set(ai):
            raise AuditWriteError("AI assessment must not claim internal reasoning")
        for key in (
            "verdict",
            "stated_justification",
            "citations",
            "provider",
            "model",
            "prompt_version",
            "settings",
            "label_probability",
            "probability_unavailable_reason",
        ):
            if key not in ai:
                raise AuditWriteError(f"AI assessment missing {key}")
        if ai["label_probability"] is not None and ai.get("provider") != "openai":
            raise AuditWriteError(
                "A label probability can be stored only for the OpenAI model that produced it"
            )

    if final or record["completeness"] == "complete":
        if record["completeness"] != "complete":
            raise AuditWriteError("Final decision must be complete")
        if record.get("final_outcome") not in {"approved", "flagged", "needs_human_review"}:
            raise AuditWriteError("Final decision is missing a valid outcome")
        if not record.get("reconciliation_rule"):
            raise AuditWriteError("Final decision is missing the reconciliation rule")
        if not record.get("reconciliation_explanation"):
            raise AuditWriteError("Final decision does not explain the reconciliation rule")
        if not isinstance(ai, dict):
            raise AuditWriteError("Complete decision is missing the AI assessment")
        for key in ("decision_id", "version", "created_at", "updated_at"):
            if not record.get(key):
                raise AuditWriteError(f"Final decision is missing {key}")


class DecisionStore:
    """SQLite decision log. Writes are transactional. Events are append-only."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            raise AuditWriteError(f"Could not open decision log: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (str(AUDIT_SCHEMA_VERSION),),
                )
                conn.commit()
            elif int(row["value"]) != AUDIT_SCHEMA_VERSION:
                raise AuditWriteError(
                    f"Decision log schema is {row['value']}; this code supports {AUDIT_SCHEMA_VERSION}"
                )
        except AuditWriteError:
            raise
        except sqlite3.Error as exc:
            raise AuditWriteError(f"Could not initialize decision log: {exc}") from exc
        finally:
            conn.close()

    def _transaction(self, operation):
        try:
            conn = self._connect()
        except AuditWriteError:
            raise
        except sqlite3.Error as exc:
            raise AuditWriteError(f"Could not open decision log: {exc}") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except AuditWriteError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise AuditWriteError(f"Decision log write failed: {exc}") from exc
        finally:
            conn.close()

    def start_run(
        self,
        run_id: str,
        source_spreadsheet: str,
        source_policy: str,
        checker_provider: str,
    ) -> None:
        def operation(conn):
            conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    run_id, started_at, completed_at, status, schema_version,
                    source_spreadsheet, source_policy, checker_provider
                ) VALUES (?, ?, NULL, 'running', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _now(),
                    AUDIT_SCHEMA_VERSION,
                    source_spreadsheet,
                    source_policy,
                    checker_provider,
                ),
            )

        self._transaction(operation)

    def complete_run(self, run_id: str) -> None:
        def operation(conn):
            updated = conn.execute(
                """
                UPDATE runs
                SET status = 'complete', completed_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )
            if updated.rowcount != 1:
                raise AuditWriteError(f"Cannot complete unknown run {run_id}")

        self._transaction(operation)

    def append_event(
        self,
        run_id: str,
        expense_id: Optional[str],
        component: str,
        event_type: str,
        payload: dict,
        decision_id: Optional[str] = None,
    ) -> int:
        _reject_secrets(payload)

        def operation(conn):
            cursor = conn.execute(
                """
                INSERT INTO decision_events (
                    run_id, expense_id, decision_id, occurred_at, component, event_type, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    expense_id,
                    decision_id,
                    _now(),
                    component,
                    event_type,
                    _dumps(payload),
                ),
            )
            return int(cursor.lastrowid)

        return self._transaction(operation)

    def write_decision(self, record: dict) -> dict:
        """Insert the next version. Earlier versions and events stay in place."""
        return self._insert(dict(record), final=record.get("completeness") == "complete")

    def finalize_decision(self, record: dict) -> dict:
        """Mandatory final write. A failure rolls back this version and must stop the run."""
        stored = dict(record)
        stored["completeness"] = "complete"
        stored["schema_version"] = DECISION_RECORD_VERSION
        return self._insert(stored, final=True)

    def _insert(self, record: dict, *, final: bool) -> dict:
        record["schema_version"] = record.get("schema_version", DECISION_RECORD_VERSION)
        # Validate contents before assigning an id so a bad record is not stored.
        provisional = dict(record)
        provisional.setdefault("decision_id", "pending")
        provisional.setdefault("version", 1)
        provisional.setdefault("created_at", _now())
        provisional.setdefault("updated_at", provisional["created_at"])
        validate_decision_record(provisional, final=final)
        _reject_secrets(provisional)

        def operation(conn):
            current = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version, MIN(created_at) AS created_at
                FROM decision_records
                WHERE run_id = ? AND expense_id = ?
                """,
                (record["run_id"], record["expense_id"]),
            ).fetchone()
            version = int(current["version"]) + 1
            now = _now()
            stored = dict(record)
            stored["version"] = version
            stored["decision_id"] = f"{record['run_id']}:{record['expense_id']}:v{version}"
            stored["created_at"] = current["created_at"] or now
            stored["updated_at"] = now
            stored["schema_version"] = DECISION_RECORD_VERSION
            validate_decision_record(stored, final=final)
            _reject_secrets(stored)
            conn.execute(
                """
                INSERT INTO decision_records (
                    decision_id, run_id, expense_id, version, created_at, updated_at,
                    responsible_component, completeness, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["decision_id"],
                    stored["run_id"],
                    stored["expense_id"],
                    stored["version"],
                    stored["created_at"],
                    stored["updated_at"],
                    stored["responsible_component"],
                    stored["completeness"],
                    _dumps(stored),
                ),
            )
            conn.execute(
                """
                INSERT INTO decision_events (
                    run_id, expense_id, decision_id, occurred_at, component, event_type, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["run_id"],
                    stored["expense_id"],
                    stored["decision_id"],
                    now,
                    stored["responsible_component"],
                    "decision_recorded",
                    _dumps(
                        {
                            "decision_id": stored["decision_id"],
                            "version": stored["version"],
                            "final_outcome": stored.get("final_outcome"),
                            "completeness": stored["completeness"],
                            "reconciliation_rule": stored.get("reconciliation_rule"),
                        }
                    ),
                ),
            )
            return stored

        return self._transaction(operation)

    def record_human_review(
        self,
        run_id: str,
        expense_id: str,
        reviewer: str,
        note: str,
        outcome: str,
        reconciliation_rule: str,
    ) -> dict:
        """Record an actual human review. This is never inferred by the pipeline."""
        reviewer = (reviewer or "").strip()
        note = (note or "").strip()
        if not reviewer or not note:
            raise AuditWriteError("A human review requires the reviewer and the note they entered")
        if outcome not in {"approved", "flagged", "needs_human_review"}:
            raise AuditWriteError("Human review outcome is not a recognized status")
        if not reconciliation_rule:
            raise AuditWriteError("Human review requires the reconciliation rule that was applied")

        latest = self.get_latest(run_id, expense_id)
        if latest is None:
            raise AuditWriteError(f"No decision exists to review for {expense_id}")

        revised = dict(latest)
        review = {
            "reviewer": reviewer,
            "note": note,
            "outcome": outcome,
            "recorded_at": _now(),
        }
        revised["human_review"] = review
        revised["final_outcome"] = outcome
        revised["reconciliation_rule"] = reconciliation_rule
        from .approver import explain_reconciliation

        revised["reconciliation_explanation"] = explain_reconciliation(reconciliation_rule)
        revised["responsible_component"] = "human_reviewer"
        revised["overrides"] = list(latest.get("overrides") or []) + [
            {
                "prior_decision_id": latest["decision_id"],
                "prior_outcome": latest.get("final_outcome"),
                "new_outcome": outcome,
                "reconciliation_rule": reconciliation_rule,
            }
        ]
        revised["completeness"] = "complete"

        def operation(conn):
            stored = self._insert_with_connection(conn, revised, final=True)
            self._append_event_with_connection(
                conn,
                run_id,
                expense_id,
                "human_reviewer",
                "human_review",
                review,
                decision_id=stored["decision_id"],
            )
            return stored

        return self._transaction(operation)

    def _insert_with_connection(self, conn, record: dict, *, final: bool) -> dict:
        """Insert a version using an existing transaction. Used by human review."""
        current = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS version, MIN(created_at) AS created_at
            FROM decision_records
            WHERE run_id = ? AND expense_id = ?
            """,
            (record["run_id"], record["expense_id"]),
        ).fetchone()
        version = int(current["version"]) + 1
        now = _now()
        stored = dict(record)
        stored["version"] = version
        stored["decision_id"] = f"{record['run_id']}:{record['expense_id']}:v{version}"
        stored["created_at"] = current["created_at"] or now
        stored["updated_at"] = now
        stored["schema_version"] = DECISION_RECORD_VERSION
        validate_decision_record(stored, final=final)
        _reject_secrets(stored)
        conn.execute(
            """
            INSERT INTO decision_records (
                decision_id, run_id, expense_id, version, created_at, updated_at,
                responsible_component, completeness, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored["decision_id"],
                stored["run_id"],
                stored["expense_id"],
                stored["version"],
                stored["created_at"],
                stored["updated_at"],
                stored["responsible_component"],
                stored["completeness"],
                _dumps(stored),
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_events (
                run_id, expense_id, decision_id, occurred_at, component, event_type, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored["run_id"],
                stored["expense_id"],
                stored["decision_id"],
                now,
                stored["responsible_component"],
                "decision_recorded",
                _dumps(
                    {
                        "decision_id": stored["decision_id"],
                        "version": stored["version"],
                        "final_outcome": stored.get("final_outcome"),
                        "completeness": stored["completeness"],
                        "reconciliation_rule": stored.get("reconciliation_rule"),
                    }
                ),
            ),
        )
        return stored

    def _append_event_with_connection(
        self,
        conn,
        run_id: str,
        expense_id: Optional[str],
        component: str,
        event_type: str,
        payload: dict,
        decision_id: Optional[str] = None,
    ) -> None:
        _reject_secrets(payload)
        conn.execute(
            """
            INSERT INTO decision_events (
                run_id, expense_id, decision_id, occurred_at, component, event_type, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, expense_id, decision_id, _now(), component, event_type, _dumps(payload)),
        )

    def get_latest(self, run_id: str, expense_id: str) -> Optional[dict]:
        return self.get_version(run_id, expense_id, None)

    def get_version(self, run_id: str, expense_id: str, version: Optional[int]) -> Optional[dict]:
        conn = self._connect()
        try:
            if version is None:
                row = conn.execute(
                    """
                    SELECT payload_json FROM decision_records
                    WHERE run_id = ? AND expense_id = ?
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (run_id, expense_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT payload_json FROM decision_records
                    WHERE run_id = ? AND expense_id = ? AND version = ?
                    """,
                    (run_id, expense_id, version),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AuditWriteError(f"Decision log read failed: {exc}") from exc
        finally:
            conn.close()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def list_events(self, run_id: str, expense_id: Optional[str] = None) -> list[dict]:
        conn = self._connect()
        try:
            if expense_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM decision_events
                    WHERE run_id = ?
                    ORDER BY event_id ASC
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM decision_events
                    WHERE run_id = ? AND (expense_id = ? OR expense_id IS NULL)
                    ORDER BY event_id ASC
                    """,
                    (run_id, expense_id),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AuditWriteError(f"Decision log read failed: {exc}") from exc
        finally:
            conn.close()
        events = []
        for row in rows:
            events.append(
                {
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "expense_id": row["expense_id"],
                    "decision_id": row["decision_id"],
                    "occurred_at": row["occurred_at"],
                    "component": row["component"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
        return events
