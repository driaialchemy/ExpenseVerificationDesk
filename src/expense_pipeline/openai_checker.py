"""OpenAI checker mode.

Ask for a short label, A or B, then ask for a separate explanation.
A maps to approved and B maps to flagged. Those words are not assumed to be
single tokens: on o200k_base, flagged is two tokens. A probability is stored
only after tiktoken confirms the configured model's encoding uses one token
for the returned label, and only when that token's logprob is finite and not
positive. Scores are observational and do not change the verdict.

Chat Completions logprobs are documented at
https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create/
finish_reason is stop, length, tool_calls, content_filter, or function_call.
A logprob of -9999.0 is the documented sentinel for a very unlikely token.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Optional

import httpx

from .audit import AuditWriteError
from .decision_builder import (
    applicable_clause_ids,
    citation_coverage,
    model_input_facts,
    render_daily_context,
)
from .policy_parser import normalize_category
from .schemas import CheckerOutput, Expense, ExpenseVerdict, PolicyRules
from .verifier import daily_group_key

# Explicit short labels. Do not score the words approved or flagged themselves.
LABEL_MAP = {"A": "approved", "B": "flagged"}
PROMPT_VERSION = "openai-label-ab-v1"
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


def verify_label_tokenization(model: str) -> dict:
    """Confirm A and B are each one token for this model's tiktoken encoding."""
    try:
        import tiktoken

        encoding = tiktoken.encoding_for_model(model)
    except Exception:
        return {
            "verified": False,
            "encoding": None,
            "counts": {},
            "reason": "tokenizer_unverified_for_model",
            "model": model,
        }
    counts = {label: len(encoding.encode(label)) for label in LABEL_MAP}
    verified = all(count == 1 for count in counts.values())
    return {
        "verified": verified,
        "encoding": encoding.name,
        "counts": counts,
        "reason": None if verified else "label_not_single_token_for_model",
        "model": model,
    }


def interpret_label_choice(choice: dict, verification: Optional[dict] = None) -> dict:
    """Map A/B to approved/flagged and keep a score only when the token is valid."""
    verification = verification or {
        "verified": False,
        "reason": "tokenizer_unverified_for_model",
        "counts": {},
    }
    message = choice.get("message") or {}
    refusal = message.get("refusal")
    finish = choice.get("finish_reason")
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""
    result = {
        "label": None,
        "raw_label": text or None,
        "label_mapping": dict(LABEL_MAP),
        "output_status": "invalid",
        "finish_reason": finish,
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
    if finish in {"tool_calls", "function_call"}:
        result["probability_unavailable_reason"] = "finish_reason_tool_call"
        return result

    mapped = LABEL_MAP.get(text)
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

    if finish == "length" and mapped is None:
        result["probability_unavailable_reason"] = "truncated"
        return result
    if mapped is None:
        result["probability_unavailable_reason"] = (
            result["probability_unavailable_reason"] or "label_not_in_mapping"
        )
        return result

    result["label"] = mapped
    result["output_status"] = "ok"
    if not verification.get("verified"):
        result["probability_unavailable_reason"] = verification.get("reason") or (
            "tokenizer_unverified_for_model"
        )
        return result
    if verification.get("counts", {}).get(text) != 1:
        result["probability_unavailable_reason"] = "label_not_single_token_for_model"
        return result
    if not tokens:
        result["probability_unavailable_reason"] = (
            result["probability_unavailable_reason"] or "logprobs_unavailable"
        )
        return result
    if len(tokens) != 1:
        result["label_logprob"] = None
        result["label_probability"] = None
        result["probability_unavailable_reason"] = "label_spans_multiple_tokens"
        return result

    token = tokens[0]
    token_text = str(token.get("token", ""))
    result["token_text"] = token_text
    result["alternatives"] = _alternatives(token.get("top_logprobs") or [])
    if token_text.strip() != text:
        result["label"] = None
        result["output_status"] = "invalid"
        result["probability_unavailable_reason"] = "token_text_does_not_match_label"
        return result

    accepted, reason = _accepted_logprob(token.get("logprob"))
    if accepted is None:
        result["probability_unavailable_reason"] = reason
        return result

    result["label_logprob"] = accepted
    result["label_probability"] = probability_from_logprob(accepted)
    result["probability_unavailable_reason"] = None
    return result


def _accepted_logprob(logprob):
    """Return a finite logprob that is not positive. Do not coerce other values."""
    if isinstance(logprob, bool) or not isinstance(logprob, (int, float)):
        return None, "label_token_logprob_missing"
    if not math.isfinite(logprob):
        return None, "logprob_nonfinite"
    if logprob > 0:
        return None, "logprob_positive"
    return float(logprob), None


def check_compliance_openai(
    expenses: list[Expense],
    policy_rules: PolicyRules,
    model: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    audit=None,
    run_id: Optional[str] = None,
) -> CheckerOutput:
    """Classify every expense, then request a separate explanation. Scores are observational."""
    resolved_model = resolve_openai_model(model)
    verification = verify_label_tokenization(resolved_model)
    owns_client = client is None
    if owns_client:
        client = build_openai_client()

    classify_max_tokens = 1 if verification["verified"] else 8
    settings = {
        "classify_temperature": 0,
        "classify_max_tokens": classify_max_tokens,
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS,
        "explain_temperature": 0,
        "explain_max_tokens": EXPLAIN_MAX_TOKENS,
        "label_mapping": dict(LABEL_MAP),
        "tokenization": verification,
        "logprobs_reference": (
            "https://developers.openai.com/api/reference/resources/chat/"
            "subresources/completions/methods/create/"
        ),
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
                skipped = {
                    "report_id": expense.report_id,
                    "output_status": "not_attempted",
                    "error": "Stopped after an earlier invalid or refused label",
                }
                output.errors.append(skipped)
                _emit(audit, run_id, expense.report_id, "error", skipped)
                continue
            facts = {
                **model_input_facts(expense, expenses),
                "prompt_row_ids": [
                    row["report_id"] for row in model_input_facts(expense, expenses)["rows"]
                ],
            }
            request_meta = {
                "provider": "openai",
                "endpoint": "/chat/completions",
                "requested_model": resolved_model,
                "prompt_version": PROMPT_VERSION,
                "settings": settings,
                "facts_received": facts,
            }
            try:
                _emit(audit, run_id, expense.report_id, "request_started", request_meta)
                choice, call_retries, response_model = _classify(
                    client,
                    resolved_model,
                    expense,
                    expenses,
                    policy_rules,
                    classify_max_tokens,
                    on_retry=lambda payload: _emit(
                        audit, run_id, expense.report_id, "retry", payload
                    ),
                )
                output.retries.extend(call_retries)
            except AuditWriteError:
                raise
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
                _emit(
                    audit,
                    run_id,
                    expense.report_id,
                    "error",
                    {"output_status": "unavailable", "error": message},
                )
                continue

            interpreted = interpret_label_choice(choice, verification)
            interpreted["response_model"] = response_model
            _emit(
                audit,
                run_id,
                expense.report_id,
                "classification_observed",
                {
                    "raw_label": interpreted.get("raw_label"),
                    "verdict": interpreted.get("label"),
                    "label_mapping": interpreted.get("label_mapping"),
                    "output_status": interpreted.get("output_status"),
                    "finish_reason": interpreted.get("finish_reason"),
                    "label_logprob": interpreted.get("label_logprob"),
                    "label_probability": interpreted.get("label_probability"),
                    "probability_unavailable_reason": interpreted.get(
                        "probability_unavailable_reason"
                    ),
                    "token_text": interpreted.get("token_text"),
                    "token_count": interpreted.get("token_count"),
                    "original_text": interpreted.get("raw_content"),
                    "facts_received": facts,
                    "requested_model": resolved_model,
                    "response_model": response_model,
                    "provider": "openai",
                },
            )
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
                _emit(
                    audit,
                    run_id,
                    expense.report_id,
                    "error",
                    {
                        "output_status": interpreted["output_status"],
                        "error": interpreted["probability_unavailable_reason"],
                        "original_text": interpreted.get("raw_content"),
                    },
                )
                continue

            explanation_status = "ok"
            stated: list[str] = []
            citations: list[str] = []
            original_explanation = None
            try:
                explained, explain_retries, explain_model = _explain(
                    client,
                    resolved_model,
                    expense,
                    expenses,
                    policy_rules,
                    interpreted["label"],
                    on_retry=lambda payload: _emit(
                        audit, run_id, expense.report_id, "retry", payload
                    ),
                )
                output.retries.extend(explain_retries)
                original_explanation = (explained.get("message") or {}).get("content")
                stated, citations, explanation_status = _parse_explanation(explained)
                _emit(
                    audit,
                    run_id,
                    expense.report_id,
                    "explanation_observed",
                    {
                        "original_text": original_explanation,
                        "explanation_status": explanation_status,
                        "stated_justification": stated,
                        "citations": citations,
                        "response_model": explain_model,
                        "provider": "openai",
                        "requested_model": resolved_model,
                    },
                )
            except AuditWriteError:
                raise
            except Exception as exc:
                explanation_status = "invalid"
                failure = {
                    "report_id": expense.report_id,
                    "output_status": "explanation_invalid",
                    "error": _redact(str(exc)),
                }
                output.errors.append(failure)
                _emit(audit, run_id, expense.report_id, "error", failure)

            coverage = citation_coverage(citations, applicable_clause_ids(expense, policy_rules))
            complete = coverage["complete"] and explanation_status == "ok"
            assessment = _assessment(
                expense.report_id,
                resolved_model,
                settings,
                interpreted,
                stated_justification=stated,
                citations=citations,
                citations_complete=complete,
                citation_coverage=coverage,
                explanation_status=explanation_status,
                original_text=original_explanation,
                facts_received=facts,
                response_model=response_model,
            )
            _emit(audit, run_id, expense.report_id, "ai_stated_output", assessment)
            output.assessments.append(assessment)
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
    citation_coverage: Optional[dict] = None,
    explanation_status: str,
    original_text=None,
    facts_received: Optional[dict] = None,
    response_model: Optional[str] = None,
) -> dict:
    return {
        "kind": "model_stated_output",
        "note": AI_NOTE,
        "report_id": report_id,
        "verdict": interpreted.get("label"),
        "stated_justification": stated_justification,
        "citations": citations,
        "citations_complete": citations_complete,
        "citation_coverage": citation_coverage,
        "original_text": original_text if original_text is not None else interpreted.get("raw_content"),
        "facts_received": facts_received,
        "label_mapping": dict(LABEL_MAP),
        "provider": "openai",
        "model": response_model or model,
        "requested_model": model,
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
        accepted, _reason = _accepted_logprob(item.get("logprob"))
        if "token" not in item or accepted is None:
            continue
        alternatives.append(
            {
                "token": item["token"],
                "logprob": accepted,
                "probability": probability_from_logprob(accepted),
            }
        )
    return alternatives


def _classify(client, model, expense, expenses, policy_rules, max_tokens, on_retry=None):
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "logprobs": True,
        "top_logprobs": TOP_LOGPROBS,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Classify this expense using the daily context. "
                    "Reply with exactly one label: A or B. "
                    "A means approved. B means flagged. Do not add any other text."
                ),
            },
            {
                "role": "user",
                "content": _expense_prompt(expense, expenses, policy_rules),
            },
        ],
    }
    return _post_chat(client, payload, expense.report_id, "classify", on_retry)


def _explain(client, model, expense, expenses, policy_rules, label, on_retry=None):
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
                    "Cite every clause that applies to this expense's category. "
                    "Do not invent a citation and do not output a replacement label."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Decided label: {label}\n"
                    f"{_expense_prompt(expense, expenses, policy_rules)}\n"
                    'Return {"reasons": ["..."], "rule_citations": ["category.clause"]}'
                ),
            },
        ],
    }
    choice, retries, response_model = _post_chat(
        client, payload, expense.report_id, "explain", on_retry
    )
    return choice, retries, response_model


def _post_chat(client, payload: dict, report_id: str, call: str, on_retry=None):
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
            return choice, retries, body.get("model")
        except AuditWriteError:
            raise
        except Exception as exc:
            retriable = attempt == 1 and _is_transient(exc)
            record = {
                "report_id": report_id,
                "component": "openai_checker",
                "call": call,
                "attempt": attempt,
                "retried": retriable,
                "requested_model": payload.get("model"),
                "endpoint": "/chat/completions",
                "error": _redact(str(exc)),
            }
            retries.append(record)
            if on_retry is not None:
                on_retry(record)
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


def _emit(audit, run_id, expense_id, event_type: str, payload: dict) -> None:
    if audit is None:
        return
    if not run_id:
        raise AuditWriteError("A decision-log write requires a run id")
    audit.append_event(run_id, expense_id, "checker", event_type, payload)


def _expense_prompt(expense: Expense, expenses: list[Expense], policy_rules: PolicyRules) -> str:
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
        render_daily_context(
            [item for item in expenses if daily_group_key(item) == daily_group_key(expense)]
        ),
        "Policy thresholds for this category:",
    ]
    category_key = daily_group_key(expense)[2]
    for category, rule in policy_rules.rules.items():
        if normalize_category(category) != category_key:
            continue
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
