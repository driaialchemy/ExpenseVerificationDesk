"""Tests for Stage 3: Compliance Checker (LLM-assisted)."""

import pytest
import json
from unittest.mock import Mock, patch

from src.expense_pipeline.schemas import Expense, PolicyRule, PolicyRules
from src.expense_pipeline.checker import check_compliance


def create_test_expenses():
    """Create test expense data."""
    return [
        Expense(
            report_id="EXP-0001",
            employee="Alice",
            department="Engineering",
            date="2024-01-15",
            category="meals",
            amount=50.00,
            currency="USD",
            receipt_attached=True,
        ),
        Expense(
            report_id="EXP-0002",
            employee="Bob",
            department="Sales",
            date="2024-01-15",
            category="meals",
            amount=100.00,
            currency="USD",
            receipt_attached=False,
        ),
    ]


def create_test_policy():
    """Create test policy rules."""
    rules = {
        "meals": PolicyRule(
            category="meals",
            daily_limit=75.00,
            receipt_required_above=30.00,
        ),
        "travel": PolicyRule(
            category="travel",
            daily_limit=500.00,
            receipt_required_above=150.00,
        ),
    }
    return PolicyRules(rules=rules, source_file="test.yaml")


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_basic(mock_anthropic_class):
    """Test basic compliance checking with mocked LLM."""
    expenses = create_test_expenses()
    policy = create_test_policy()

    # Mock LLM response
    mock_response_data = [
        {
            "report_id": "EXP-0001",
            "verdict": "approved",
            "reasons": [],
            "rule_citations": [],
        },
        {
            "report_id": "EXP-0002",
            "verdict": "flagged",
            "reasons": ["missing receipt"],
            "rule_citations": ["meals.receipt_required_above"],
        },
    ]

    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client

    mock_message = Mock()
    mock_message.content = [Mock(text=json.dumps(mock_response_data))]
    mock_client.messages.create.return_value = mock_message

    output = check_compliance(expenses, policy)

    assert len(output.verdicts) == 2
    assert output.verdicts[0].report_id == "EXP-0001"
    assert output.verdicts[0].verdict == "approved"
    assert output.verdicts[1].verdict == "flagged"
    assert output.assessments[0]["citations_complete"] is False
    assert output.assessments[1]["citations_complete"] is True
    assert output.assessments[0]["kind"] == "model_stated_output"
    assert output.assessments[0]["label_probability"] is None


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_row_count_parity(mock_anthropic_class):
    """Test that checker preserves row count."""
    expenses = create_test_expenses()
    policy = create_test_policy()

    mock_response_data = [
        {
            "report_id": "EXP-0001",
            "verdict": "approved",
            "reasons": [],
            "rule_citations": [],
        },
        {
            "report_id": "EXP-0002",
            "verdict": "approved",
            "reasons": [],
            "rule_citations": [],
        },
    ]

    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client

    mock_message = Mock()
    mock_message.content = [Mock(text=json.dumps(mock_response_data))]
    mock_client.messages.create.return_value = mock_message

    output = check_compliance(expenses, policy)

    assert len(output.verdicts) == len(expenses)


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_missing_verdict(mock_anthropic_class):
    """Test gate failure when LLM output is missing a verdict."""
    expenses = create_test_expenses()
    policy = create_test_policy()

    # Missing verdict for second expense
    mock_response_data = [
        {
            "report_id": "EXP-0001",
            "verdict": "approved",
            "reasons": [],
            "rule_citations": [],
        },
    ]

    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client

    mock_message = Mock()
    mock_message.content = [Mock(text=json.dumps(mock_response_data))]
    mock_client.messages.create.return_value = mock_message

    with pytest.raises(ValueError) as exc_info:
        check_compliance(expenses, policy)

    assert "gate failed" in str(exc_info.value).lower()


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_invalid_json(mock_anthropic_class):
    """Test error handling for invalid JSON response from LLM."""
    expenses = create_test_expenses()
    policy = create_test_policy()

    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client

    mock_message = Mock()
    mock_message.content = [Mock(text="This is not valid JSON")]
    mock_client.messages.create.return_value = mock_message

    with pytest.raises(ValueError) as exc_info:
        check_compliance(expenses, policy)

    assert "Failed to parse" in str(exc_info.value)


def test_resolve_model_default(monkeypatch):
    from src.expense_pipeline.checker import DEFAULT_MODEL, resolve_model

    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert resolve_model() == DEFAULT_MODEL
    assert resolve_model("claude-haiku-4-5") == "claude-haiku-4-5"


def test_resolve_model_from_env(monkeypatch):
    from src.expense_pipeline.checker import resolve_model

    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
    assert resolve_model() == "claude-opus-5"
    assert resolve_model("claude-sonnet-5") == "claude-sonnet-5"


def test_build_client_strips_api_key_whitespace(monkeypatch):
    from src.expense_pipeline.checker import _build_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", '  "sk-ant-testkey"  ')
    with patch("src.expense_pipeline.checker.Anthropic") as mock_cls:
        _build_client()
        assert mock_cls.call_args.kwargs["api_key"] == "sk-ant-testkey"


def test_exception_chain_redacts_api_key():
    from src.expense_pipeline.checker import _exception_chain

    exc = Exception("Illegal header value b' sk-ant-api03-SECRETVALUE '")
    text = _exception_chain(exc)
    assert "SECRETVALUE" not in text
    assert "sk-ant-[REDACTED]" in text


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_illegal_header_does_not_leak_key(mock_anthropic_class):
    from src.expense_pipeline.checker import AnthropicConnectionError

    expenses = create_test_expenses()
    policy = create_test_policy()
    secret = "sk-ant-api03-SECRETVALUE"
    illegal = Exception(f"Illegal header value b' {secret} '")
    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client
    mock_client.messages.create.side_effect = illegal

    with pytest.raises(AnthropicConnectionError) as exc_info:
        check_compliance(expenses, policy)

    message = str(exc_info.value)
    assert secret not in message
    assert "whitespace" in message.lower()


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_uses_requested_model(mock_anthropic_class):
    expenses = create_test_expenses()
    policy = create_test_policy()

    mock_response_data = [
        {"report_id": "EXP-0001", "verdict": "approved", "reasons": [], "rule_citations": []},
        {"report_id": "EXP-0002", "verdict": "approved", "reasons": [], "rule_citations": []},
    ]
    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps(mock_response_data))]
    )

    check_compliance(expenses, policy, model="claude-haiku-4-5")

    assert mock_client.messages.create.call_args.kwargs["model"] == "claude-haiku-4-5"


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_falls_back_when_model_missing(mock_anthropic_class):
    expenses = create_test_expenses()
    policy = create_test_policy()

    mock_response_data = [
        {"report_id": "EXP-0001", "verdict": "approved", "reasons": [], "rule_citations": []},
        {"report_id": "EXP-0002", "verdict": "approved", "reasons": [], "rule_citations": []},
    ]
    not_found = Exception("404 {type: not_found_error} model: claude-3-5-sonnet-20241022")
    not_found.status_code = 404

    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client
    mock_client.messages.create.side_effect = [
        not_found,
        Mock(content=[Mock(text=json.dumps(mock_response_data))]),
    ]
    mock_client.models.list.return_value = Mock(
        data=[Mock(id="claude-sonnet-5"), Mock(id="claude-haiku-4-5")]
    )

    output = check_compliance(expenses, policy, model="claude-3-5-sonnet-20241022")

    assert len(output.verdicts) == 2
    assert mock_client.messages.create.call_args_list[1].kwargs["model"] == "claude-sonnet-5"


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_model_not_found_message(mock_anthropic_class):
    expenses = create_test_expenses()
    policy = create_test_policy()

    not_found = Exception("404 {type: not_found_error}")
    not_found.status_code = 404

    mock_client = Mock()
    mock_anthropic_class.return_value = mock_client
    mock_client.messages.create.side_effect = not_found
    mock_client.models.list.return_value = Mock(data=[])

    with pytest.raises(ValueError) as exc_info:
        check_compliance(expenses, policy, model="claude-opus-4-1")

    message = str(exc_info.value)
    assert "claude-opus-4-1" in message
    assert "ANTHROPIC_MODEL" in message


def _connection_error(message="Connection error."):
    exc = Exception(message)
    exc.status_code = None
    return exc


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_retries_over_ipv4(mock_anthropic_class):
    expenses = create_test_expenses()
    policy = create_test_policy()
    mock_response_data = [
        {"report_id": "EXP-0001", "verdict": "approved", "reasons": [], "rule_citations": []},
        {"report_id": "EXP-0002", "verdict": "approved", "reasons": [], "rule_citations": []},
    ]

    failing_client = Mock()
    failing_client.messages.create.side_effect = _connection_error()
    ok_client = Mock()
    ok_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps(mock_response_data))]
    )
    mock_anthropic_class.side_effect = [failing_client, ok_client]

    output = check_compliance(expenses, policy)

    assert len(output.verdicts) == 2
    assert mock_anthropic_class.call_count == 2


@patch("src.expense_pipeline.checker.Anthropic")
def test_check_compliance_connection_error_includes_cause(mock_anthropic_class):
    from src.expense_pipeline.checker import AnthropicConnectionError

    expenses = create_test_expenses()
    policy = create_test_policy()

    failing_client = Mock()
    failing_client.messages.create.side_effect = _connection_error()
    mock_anthropic_class.return_value = failing_client

    with pytest.raises(AnthropicConnectionError) as exc_info:
        check_compliance(expenses, policy)

    assert "Underlying error" in str(exc_info.value)
    assert "--skip-checker" in str(exc_info.value)
