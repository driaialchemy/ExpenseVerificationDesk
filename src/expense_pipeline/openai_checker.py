"""OpenAI checker mode.

Classify each expense as the single token `approved` or `flagged`, then ask for a
separate explanation. Label-token logprobs are recorded for observation only.
They are not an approval threshold and they do not change the verdict.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Optional

import httpx

from .decision_builder import citations_are_complete, known_clause_ids
from .schemas import CheckerOutput, Expense, ExpenseVerdict, PolicyRules

LABELS = ("approved", "flagged")
PROMPT_VERSION = "openai-label-then-explain-v1"
CLASSIFY_MAX_TOKENS = 1
EXPLAIN_MAX_TOKENS = 400
TOP_LOGPROBS = 5
AI_NOTE = (
    "stated_justification is text the model produced. "
    "It is not an observation of the model's internal reasoning."
)

_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


class CheckerOutputError(ValueError):
    """The checker could not produce a valid label. Finalization must stop."""

    def __init__(self, message: str, partial: CheckerOutput):
        super().__init__(message)
        self.partial = partial


def probability_from_logprob(logprob: float) -> float:
    """Convert a natural-log token probability with exp(logprob). No threshold is applied."""
    return math.exp(logprob)


def resolve_openai_model(model: Optional[str] = None) -> str:
    if model and model.strip():
        return model.strip()
    configured = os.environ.get("OPENAI_MODEL", "")
    if configured.strip():
        return configured.strip()
    raise ValueError(
        "OpenAI checker mode requires OPENAI_MODEL or --model set to a model "
        "whose endpoint returns token logprobs."
    )


def build_openai_client() -> httpx.Client:
    raw = os.environ.get("OPENAI_API_KEY")
    if raw is None or not raw.strip():
        raise ValueError("OPENAI_API_KEY is not set")
    api_key = raw.strip().strip('"').strip("'").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,
    )


def interpret_label_choice(choice: dict) -> dict:
    """Read the emitted label and its actual token logprobs. Do not invent a score."""
    message = choice.get("message") or {}
    refusal = message.get("refusal")
    finish = choice.get("finish_reason")
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""
    result = {
        "label": None,
        "output_status": "invalid",
        "token_text": None,
        "token_count": 0,
        "label_logprob": None,
        "label_probability": None,
        "probability_unavailable_reason": None,
        "alternatives": [],
        "raw_content": content if isinstance(content, str) else None,
    }

    if refusal or finish == "content_filter":
        result["output_status"] = "refusal"
        result["probability_unavailable_reason"] = "model_refusal"
        return result

    logprobs = choice.get("logprobs")
    tokens = []
    if isinstance(logprobs, dict) and isinstance(logprobs.get("content"), list):
        tokens = [token for token in logprobs["content"] if isinstance(token, dict)]
    elif logprobs is None:
        result["probability_unavailable_reason"] = "logprobs_unavailable"
    else:
        result["probability_unavailable_reason"] = "logprobs_unrecognized"

    result["token_count"] = len(tokens)
    if tokens:
        result["token_text"] = "".join(str(token.get("token", "")) for token in tokens)

    if text not in LABELS:
        result["output_status"] = "invalid"
        result["probability_unavailable_reason"] = (
            result["probability_unavailable_reason"] or "label_not_approved_or_flagged"
        )
        return result

    result["label"] = text
    result["output_status"] = "ok"

    if not tokens:
        result["probability_unavailable_reason"] = (
            result["probability_unavailable_reason"] or "logprobs_unavailable"
        )
        return result
    if len(tokens) != 1:
        result["probability_unavailable_reason"] = "label_spans_multiple_tokens"
        return result

    token = tokens[0]
    token_text = str(token.get("token", ""))
    result["token_text"] = token_text
    result["alternatives"] = _alternatives(token.get("top_logprobs") or [])
    if token_text.strip() != text:
        result["label"] = None
        result["output_status"] = "invalid"
        result["label_logprob"] = None
        result["label_probability"] = None
        result["probability_unavailable_reason"] = "token_text_does_not_match_label"
        return result

    logprob = token.get("logprob")
    if not isinstance(logprob, (int, float)):
        result["probability_unavailable_reason"] = "label_token_logprob_missing"
        return result

    result["label_logprob"] = float(logprob)
    result["label_probability"] = probability_from_logprob(float(logprob))
    result["probability_unavailable_reason"] = None
    return result


def check_compliance_openai(
    expenses: list[Expense],
    policy_rules: PolicyRules,
    model: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> CheckerOutput:
    """Classify every expense, then request a separate explanation. Scores are observational."""
    resolved_model = resolve_openai_model(model)
    owns_client = client is None
    if owns_client:
        client = build_openai_client()

    clause_ids = known_clause_ids(policy_rules)
    settings = {
        "classify_temperature": 0,
        "classify_max_tokens": CLASSIFY_MAX_TOKENS,
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS,
        "explain_temperature": 0,
        "explain_max_tokens": EXPLAIN_MAX_TOKENS,
    }
    output = CheckerOutput(
        verdicts=[],
        provider="openai",
        model=resolved_model,
        prompt_version=PROMPT_VERSION,
        settings=settings,
    )
    blocking: Optional[str] = None

    try:
        for index, expense in enumerate(expenses):
            if blocking:
                output.errors.append(
                    {
                        "report_id": expense.report_id,
                        "output_status": "not_attempted",
                        "error": "Stopped after an earlier invalid or refused label",
                    }
                )
                continue
            try:
                choice, call_retries = _classify(client, resolved_model, expense, policy_rules)
                output.retries.extend(call_retries)
            except Exception as exc:
                message = _redact(str(exc))
                output.errors.append(
                    {
                        "report_id": expense.report_id,
                        "output_status": "unavailable",
                        "error": message,
                    }
                )
                output.assessments.append(
                    _assessment(
                        expense.report_id,
                        resolved_model,
                        settings,
                        {
                            "label": None,
                            "output_status": "unavailable",
                            "label_logprob": None,
                            "label_probability": None,
                            "probability_unavailable_reason": "request_failed",
                            "alternatives": [],
                            "token_text": None,
                            "token_count": None,
                        },
                        stated_justification=[],
                        citations=[],
                        citations_complete=False,
                        explanation_status="not_requested",
                    )
                )
                blocking = message
                continue

            interpreted = interpret_label_choice(choice)
            if interpreted["output_status"] != "ok" or not interpreted["label"]:
                output.assessments.append(
                    _assessment(
                        expense.report_id,
                        resolved_model,
                        settings,
                        interpreted,
                        stated_justification=[],
                        citations=[],
                        citations_complete=False,
                        explanation_status="not_requested",
                    )
                )
                output.errors.append(
                    {
                        "report_id": expense.report_id,
                        "output_status": interpreted["output_status"],
                        "error": interpreted["probability_unavailable_reason"],
                        "raw_content": interpreted.get("raw_content"),
                    }
                )
                blocking = (
                    f"{expense.report_id} label was {interpreted['output_status']}: "
                    f"{interpreted['probability_unavailable_reason']}"
                )
                continue

            explanation_status = "ok"
            stated: list[str] = []
            citations: list[str] = []
            try:
                explained, explain_retries = _explain(
                    client,
                    resolved_model,
                    expense,
                    policy_rules,
                    interpreted["label"],
                )
                output.retries.extend(explain_retries)
                stated, citations, explanation_status = _parse_explanation(explained)
            except Exception as exc:
                explanation_status = "invalid"
                output.errors.append(
                    {
                        "report_id": expense.report_id,
                        "output_status": "explanation_invalid",
                        "error": _redact(str(exc)),
                    }
                )

            complete = citations_are_complete(citations, clause_ids) and explanation_status == "ok"
            output.assessments.append(
                _assessment(
                    expense.report_id,
                    resolved_model,
                    settings,
                    interpreted,
                    stated_justification=stated,
                    citations=citations,
                    citations_complete=complete,
                    explanation_status=explanation_status,
                )
            )
            output.verdicts.append(
                ExpenseVerdict(
                    report_id=expense.report_id,
                    verdict=interpreted["label"],
                    reasons=list(stated),
                    rule_citations=list(citations),
                )
            )
            # The loop continues. A later blocking label stops remaining calls.
            _ = index
    finally:
        if owns_client and client is not None:
            client.close()

    if blocking:
        raise CheckerOutputError(blocking, output)
    return output


def _assessment(
    report_id: str,
    model: str,
    settings: dict,
    interpreted: dict,
    *,
    stated_justification: list[str],
    citations: list[str],
    citations_complete: bool,
    explanation_status: str,
) -> dict:
    return {
        "kind": "model_stated_output",
        "note": AI_NOTE,
        "report_id": report_id,
        "verdict": interpreted.get("label"),
        "stated_justification": stated_justification,
        "citations": citations,
        "citations_complete": citations_complete,
        "provider": "openai",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "settings": settings,
        "output_status": interpreted.get("output_status"),
        "label_logprob": interpreted.get("label_logprob"),
        "label_probability": interpreted.get("label_probability"),
        "probability_unavailable_reason": interpreted.get("probability_unavailable_reason"),
        "alternatives": interpreted.get("alternatives") or [],
        "token_text": interpreted.get("token_text"),
        "token_count": interpreted.get("token_count"),
        "explanation_status": explanation_status,
    }


def _alternatives(top_logprobs: list) -> list[dict]:
    alternatives = []
    if not isinstance(top_logprobs, list):
        return alternatives
    for item in top_logprobs:
        if not isinstance(item, dict):
            continue
        logprob = item.get("logprob")
        if "token" not in item or not isinstance(logprob, (int, float)):
            continue
        alternatives.append(
            {
                "token": item["token"],
                "logprob": float(logprob),
                "probability": probability_from_logprob(float(logprob)),
            }
        )
    return alternatives


def _classify(client, model: str, expense: Expense, policy_rules: PolicyRules):
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": CLASSIFY_MAX_TOKENS,
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Classify this one expense. Reply with exactly one label token: "
                    "approved or flagged. Do not add any other text."
                ),
            },
            {"role": "user", "content": _expense_prompt(expense, policy_rules)},
        ],
    }
    return _post_chat(client, payload, expense.report_id, "classify")


def _explain(client, model: str, expense: Expense, policy_rules: PolicyRules, label: str):
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": EXPLAIN_MAX_TOKENS,
        "logprobs": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "The classification label is already decided. Explain that label. "
                    "Return JSON with keys reasons and rule_citations. "
                    "Do not output a replacement label."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Decided label: {label}\n"
                    f"{_expense_prompt(expense, policy_rules)}\n"
                    'Return {"reasons": ["..."], "rule_citations": ["category.clause"]}'
                ),
            },
        ],
    }
    body, retries = _post_chat(client, payload, expense.report_id, "explain")
    return body, retries


def _post_chat(client, payload: dict, report_id: str, call: str):
    retries = []
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.post("/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
            choice = (body.get("choices") or [None])[0]
            if not isinstance(choice, dict):
                raise ValueError(f"OpenAI {call} response did not include a choice")
            return choice, retries
        except Exception as exc:
            retriable = attempt == 1 and _is_transient(exc)
            retries.append(
                {
                    "report_id": report_id,
                    "component": "openai_checker",
                    "call": call,
                    "attempt": attempt,
                    "retried": retriable,
                    "error": _redact(str(exc)),
                }
            )
            if not retriable:
                raise


def _parse_explanation(choice: dict) -> tuple[list[str], list[str], str]:
    message = choice.get("message") or {}
    if message.get("refusal") or choice.get("finish_reason") == "content_filter":
        return [], [], "refusal"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return [], [], "invalid"
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        parsed = json.loads(content[start:end])
    except (json.JSONDecodeError, ValueError):
        return [content.strip()], [], "invalid"
    reasons = parsed.get("reasons")
    citations = parsed.get("rule_citations")
    if not isinstance(reasons, list) or not isinstance(citations, list):
        return [content.strip()], [], "invalid"
    if not all(isinstance(item, str) for item in reasons + citations):
        return [content.strip()], [], "invalid"
    return reasons, citations, "ok"


def _expense_prompt(expense: Expense, policy_rules: PolicyRules) -> str:
    lines = [
        f"report_id: {expense.report_id}",
        f"employee: {expense.employee}",
        f"department: {expense.department}",
        f"date: {expense.date}",
        f"category: {expense.category}",
        f"amount: {expense.amount}",
        f"currency: {expense.currency}",
        f"receipt_attached: {expense.receipt_attached}",
        f"notes: {expense.notes or ''}",
        "Policy thresholds:",
    ]
    for category, rule in policy_rules.rules.items():
        lines.append(
            f"- {category}: daily_limit {rule.daily_limit}; "
            f"receipt_required_above {rule.receipt_required_above}"
        )
        if rule.requires_manager_approval_above is not None:
            lines.append(
                f"  requires_manager_approval_above {rule.requires_manager_approval_above}"
            )
    lines.append(
        "Daily limits apply to the same employee, calendar day, category, and currency. "
        "Do not convert currencies."
    )
    return "\n".join(lines)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in {408, 429, 500, 502, 503, 504}


def _redact(text: str) -> str:
    return _SECRET_RE.sub("sk-[REDACTED]", text)
