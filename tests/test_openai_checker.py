"""OpenAI label-token probabilities. No live API calls."""

import math

import httpx
import pytest

from src.expense_pipeline.checker import check_compliance
from src.expense_pipeline.openai_checker import (
    CheckerOutputError,
    check_compliance_openai,
    interpret_label_choice,
    probability_from_logprob,
)
from src.expense_pipeline.schemas import Expense, PolicyRule, PolicyRules


def _expense(report_id="EXP-0001"):
    return Expense(
        report_id=report_id,
        employee="Alice",
        department="Engineering",
        date="2024-01-15",
        category="meals",
        amount=40.0,
        currency="USD",
        receipt_attached=True,
    )


def _policy():
    return PolicyRules(
        rules={"meals": PolicyRule("meals", daily_limit=75.0, receipt_required_above=30.0)},
        source_file="policy.docx",
    )


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json):
        self.calls.append({"url": url, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


VERIFIED = {
    "verified": True,
    "encoding": "o200k_base",
    "counts": {"A": 1, "B": 1},
    "reason": None,
    "model": "gpt-4o-mini",
}


def _choice(token="A", logprob=-0.693147, alternatives=None, content=None, finish_reason="stop"):
    return {
        "finish_reason": finish_reason,
        "message": {"role": "assistant", "content": content if content is not None else token},
        "logprobs": {
            "content": [
                {
                    "token": token,
                    "logprob": logprob,
                    "top_logprobs": alternatives
                    or [
                        {"token": "approved", "logprob": logprob},
                        {"token": "flagged", "logprob": -1.2},
                    ],
                }
            ]
        },
    }


def _explain(reasons=None, citations=None):
    import json

    body = json.dumps(
        {
            "reasons": reasons if reasons is not None else ["Under the daily meal limit."],
            "rule_citations": citations if citations is not None else ["meals.daily_limit"],
        }
    )
    return {
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": body},
        "logprobs": None,
    }


@pytest.fixture
def verified_tokenizer(monkeypatch):
    monkeypatch.setattr(
        "src.expense_pipeline.openai_checker.verify_label_tokenization",
        lambda model: {**VERIFIED, "model": model},
    )


def test_probability_is_exp_of_the_label_logprob():
    logprob = -0.693147
    parsed = interpret_label_choice(_choice(logprob=logprob), VERIFIED)
    assert parsed["output_status"] == "ok"
    assert parsed["label"] == "approved"
    assert parsed["raw_label"] == "A"
    assert parsed["label_logprob"] == logprob
    assert parsed["label_probability"] == pytest.approx(math.exp(logprob))
    assert parsed["label_probability"] == pytest.approx(probability_from_logprob(logprob))
    assert parsed["alternatives"][1]["token"] == "flagged"
    assert parsed["alternatives"][1]["probability"] == pytest.approx(math.exp(-1.2))
    assert parsed["probability_unavailable_reason"] is None


def test_missing_logprobs_stay_null_with_a_reason():
    choice = _choice()
    choice["logprobs"] = None
    parsed = interpret_label_choice(choice, VERIFIED)
    assert parsed["label"] == "approved"
    assert parsed["output_status"] == "ok"
    assert parsed["label_probability"] is None
    assert parsed["probability_unavailable_reason"] == "logprobs_unavailable"


def test_malformed_and_positive_logprobs_are_not_scored():
    positive = interpret_label_choice(_choice(logprob=0.2), VERIFIED)
    assert positive["label"] == "approved"
    assert positive["label_probability"] is None
    assert positive["probability_unavailable_reason"] == "logprob_positive"

    nonfinite = interpret_label_choice(_choice(logprob=float("nan")), VERIFIED)
    assert nonfinite["label_probability"] is None
    assert nonfinite["probability_unavailable_reason"] == "logprob_nonfinite"

    boolean = interpret_label_choice(_choice(logprob=True), VERIFIED)
    assert boolean["label_probability"] is None
    assert boolean["probability_unavailable_reason"] == "label_token_logprob_missing"


def test_truncation_and_complete_length_finish_are_distinct():
    truncated = interpret_label_choice(
        _choice(token="Ap", content="Ap", finish_reason="length"),
        VERIFIED,
    )
    assert truncated["label"] is None
    assert truncated["label_probability"] is None
    assert truncated["probability_unavailable_reason"] == "truncated"

    complete = interpret_label_choice(
        _choice(token="A", content="A", finish_reason="length", logprob=-0.1),
        VERIFIED,
    )
    assert complete["label"] == "approved"
    assert complete["label_probability"] == pytest.approx(math.exp(-0.1))


def test_unverified_tokenizer_keeps_the_label_and_nulls_the_score():
    parsed = interpret_label_choice(_choice())
    assert parsed["label"] == "approved"
    assert parsed["label_probability"] is None
    assert parsed["probability_unavailable_reason"] == "tokenizer_unverified_for_model"


def test_gpt4o_mini_uses_single_token_labels():
    import tiktoken

    from src.expense_pipeline.openai_checker import verify_label_tokenization

    result = verify_label_tokenization("gpt-4o-mini")
    assert result["verified"] is True
    assert result["counts"] == {"A": 1, "B": 1}
    encoding = tiktoken.encoding_for_model("gpt-4o-mini")
    assert len(encoding.encode("flagged")) == 2


def test_multi_token_label_does_not_invent_a_combined_score():
    choice = _choice(content="A")
    choice["logprobs"] = {
        "content": [
            {"token": "A", "logprob": -0.2, "top_logprobs": []},
            {"token": " ", "logprob": -0.3, "top_logprobs": []},
        ]
    }
    parsed = interpret_label_choice(choice, VERIFIED)
    assert parsed["label"] == "approved"
    assert parsed["label_probability"] is None
    assert parsed["probability_unavailable_reason"] == "label_spans_multiple_tokens"


def test_refusal_and_invalid_label_are_explicit():
    refused = interpret_label_choice(
        {
            "finish_reason": "stop",
            "message": {"content": None, "refusal": "I can't classify this"},
            "logprobs": None,
        },
        VERIFIED,
    )
    assert refused["output_status"] == "refusal"
    assert refused["label"] is None
    assert refused["label_probability"] is None
    assert refused["probability_unavailable_reason"] == "model_refusal"

    invalid = interpret_label_choice(_choice(token="maybe", content="maybe"), VERIFIED)
    assert invalid["output_status"] == "invalid"
    assert invalid["label"] is None
    assert invalid["label_probability"] is None
    assert invalid["raw_label"] == "maybe"


def test_token_text_must_match_the_label():
    choice = _choice(token="B", content="A")
    parsed = interpret_label_choice(choice, VERIFIED)
    assert parsed["output_status"] == "invalid"
    assert parsed["probability_unavailable_reason"] == "token_text_does_not_match_label"
    assert parsed["label_probability"] is None


def test_openai_mode_records_score_without_changing_the_decided_label(verified_tokenizer):
    client = FakeClient(
        [
            _Response({"choices": [_choice(logprob=-0.1053605156578263)]}),
            _Response(
                {
                    "choices": [
                        _explain(
                            reasons=["The model text says flagged, but that is only an explanation."],
                        )
                    ]
                }
            ),
        ]
    )
    output = check_compliance_openai([_expense()], _policy(), model="gpt-test", client=client)

    assert output.verdicts[0].verdict == "approved"
    assessment = output.assessments[0]
    assert assessment["provider"] == "openai"
    assert assessment["model"] == "gpt-test"
    assert assessment["label_probability"] == pytest.approx(0.9)
    assert assessment["label_mapping"] == {"A": "approved", "B": "flagged"}
    assert assessment["stated_justification"] == [
        "The model text says flagged, but that is only an explanation."
    ]
    assert client.calls[0]["json"]["max_tokens"] == 1
    assert client.calls[0]["json"]["logprobs"] is True
    assert "A means approved" in client.calls[0]["json"]["messages"][0]["content"]
    assert client.calls[1]["json"]["logprobs"] is False


def test_same_day_rows_are_in_the_openai_prompt(verified_tokenizer):
    first = _expense("EXP-0001")
    second = _expense("EXP-0002")
    client = FakeClient(
        [
            _Response({"choices": [_choice()]}),
            _Response({"choices": [_explain(citations=["meals.daily_limit", "meals.receipt_required_above"])]}),
            _Response({"choices": [_choice(token="B", content="B")]}),
            _Response({"choices": [_explain(citations=["meals.daily_limit", "meals.receipt_required_above"])]}),
        ]
    )
    output = check_compliance_openai([first, second], _policy(), model="gpt-4o-mini", client=client)
    prompt = client.calls[0]["json"]["messages"][1]["content"]
    assert "EXP-0001" in prompt and "EXP-0002" in prompt
    assert "40.0" in prompt
    assert "verifier_verdict" not in prompt
    rows = output.assessments[0]["facts_received"]["rows"]
    assert {row["report_id"] for row in rows} == {"EXP-0001", "EXP-0002"}
    assert output.assessments[0]["facts_received"]["includes_verdicts"] is False
    assert output.verdicts[1].verdict == "flagged"


def test_invalid_label_blocks_and_does_not_call_the_remaining_expense(verified_tokenizer):
    client = FakeClient([_Response({"choices": [_choice(token="unsure", content="unsure")]})])
    with pytest.raises(CheckerOutputError) as exc_info:
        check_compliance_openai(
            [_expense("EXP-0001"), _expense("EXP-0002")],
            _policy(),
            model="gpt-test",
            client=client,
        )
    partial = exc_info.value.partial
    assert partial.verdicts == []
    assert partial.assessments[0]["output_status"] == "invalid"
    assert partial.assessments[0]["label_probability"] is None
    assert partial.assessments[0]["original_text"] == "unsure"
    assert partial.errors[-1]["report_id"] == "EXP-0002"
    assert partial.errors[-1]["output_status"] == "not_attempted"
    assert len(client.calls) == 1


def test_retry_is_recorded_and_a_later_success_keeps_the_label(verified_tokenizer):
    client = FakeClient(
        [
            httpx.ConnectError("temporary"),
            _Response({"choices": [_choice(token="B", content="B", logprob=0.0)]}),
            _Response({"choices": [_explain(reasons=["Over the limit."], citations=[])]}),
        ]
    )
    output = check_compliance_openai([_expense()], _policy(), model="gpt-test", client=client)
    assert output.verdicts[0].verdict == "flagged"
    assert output.retries[0]["retried"] is True
    assert output.assessments[0]["label_probability"] == pytest.approx(1.0)
    assert output.assessments[0]["citations_complete"] is False
    assert "meals.daily_limit" in output.assessments[0]["citation_coverage"]["missing"]


def test_midway_audit_failure_keeps_the_first_expense(tmp_path, verified_tokenizer):
    from src.expense_pipeline.audit import AuditWriteError
    from src.expense_pipeline.decision_store import DecisionStore

    store = DecisionStore(tmp_path / "decision_log.sqlite")
    store.start_run("run1", "expenses.xlsx", "policy.docx", "openai")

    class Gate:
        def append_event(self, run_id, expense_id, component, event_type, payload, decision_id=None):
            if expense_id == "EXP-0002" and event_type == "classification_observed":
                raise AuditWriteError("disk full")
            return store.append_event(
                run_id, expense_id, component, event_type, payload, decision_id=decision_id
            )

    client = FakeClient(
        [
            _Response({"choices": [_choice()], "model": "gpt-4o-mini-2024-07-18"}),
            _Response({"choices": [_explain()], "model": "gpt-4o-mini-2024-07-18"}),
            _Response({"choices": [_choice()], "model": "gpt-4o-mini-2024-07-18"}),
        ]
    )
    with pytest.raises(AuditWriteError, match="disk full"):
        check_compliance_openai(
            [_expense("EXP-0001"), _expense("EXP-0002")],
            _policy(),
            model="gpt-4o-mini",
            client=client,
            audit=Gate(),
            run_id="run1",
        )

    first_events = [event["event_type"] for event in store.list_events("run1", "EXP-0001")]
    second_events = [event["event_type"] for event in store.list_events("run1", "EXP-0002")]
    assert "classification_observed" in first_events
    assert "explanation_observed" in first_events
    assert "classification_observed" not in second_events
    assert len(client.calls) == 3


def test_default_provider_stays_anthropic(monkeypatch):
    monkeypatch.delenv("EXPENSE_CHECKER_PROVIDER", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("OpenAI client should not be built")

    monkeypatch.setattr("src.expense_pipeline.openai_checker.build_openai_client", explode)
    monkeypatch.setattr(
        "src.expense_pipeline.checker._build_client",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("stop before network")),
    )
    with pytest.raises(RuntimeError, match="stop before network"):
        check_compliance([_expense()], _policy())
