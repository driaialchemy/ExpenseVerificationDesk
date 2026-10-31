version: 1
repository: expenseverificationpipeline
risk_level: medium
scope:
  summary: Expense verification pipeline with staged gates, independent verification, audit trail, and Snowflake integration points.
  allowed_paths:
    - src/expense_pipeline/
    - tests/
    - sql/
    - README.md
    - BUILD_SUMMARY.md
    - CLAUDE.md
    - AGENTS.md
    - pyproject.toml
  fixture_paths:
    - sample_data/sample_expenses.xlsx
    - sample_policy_manual.docx
  notes:
    - Fixture spreadsheets/documents are retained because tests and examples need non-secret sample inputs.
    - Do not add real expense exports, employee records, or policy manuals.
allowed_actions:
  - write_tests_for_each_stage_and_gate
  - implement_missing_stages_in_build_order
  - fix_gate_or_verifier_bugs
  - document_design_decisions
  - run_local_tests_before_completion
forbidden_actions:
  - skip_or_weaken_gate_checks
  - replace_gate_checks_with_status_strings
  - add_llm_or_network_calls_to_verifier
  - make_verifier_depend_on_checker_output_or_external_state
  - commit_credentials_or_env_files
  - hardcode_snowflake_connection_details
  - mix_stage_data_flows_improperly
  - add_out_of_scope_features_without_approval
human_approval_required_for:
  - changing_gate_semantics
  - changing_verifier_independence
  - adding_or_modifying_snowflake_credentials_or_secret_handling
  - adding_real_business_data_or_exports
  - deleting_tracked_fixtures
secrets:
  policy: Environment variables only; never commit .env, API keys, passwords, or Snowflake credentials.
  forbidden_files:
    - .env
    - .env.*
    - secrets.*
    - credentials.*
data_policy:
  real_data: forbidden
  fixtures: synthetic_or_non_sensitive_only
  exports: gitignored_unless_required_as_documented_fixture
testing:
  required:
    - stage_tests
    - gate_pass_and_fail_tests
    - mocked_llm_and_snowflake_unit_tests
    - end_to_end_tests_with_temp_files_and_no_real_snowflake

# Audit remediation metadata
audit_path: audit/multi_repo/2026-09-18/repo_expenseverificationpipeline_20260918T004902Z.json
risk_level: HIGH
human_review_required: true
audit_findings:
  - "Spreadsheet/data export files found: sample_data/sample_expenses.xlsx"
  - "External AI API usage: anthropic — API key management required."
  - "Credential pattern indicators in code: api_key"
  - "Risk level is HIGH, expected LOW."
  - "Dependency manifests present: pyproject.toml"
  - "Test suite present — actively developed project."
  - "Missing AGENTS.md — no machine-readable safety policy."

## Approval gates

Human approval is required before dependency changes, external-call changes, deploy or container config, secrets, and AI-provider or database config.

## Setup

pip install -e ".[dev]"
python -m pytest -q

## Sample data

`sample_data/sample_expenses.xlsx` is the synthetic expense fixture (7 repeated employees, 4 departments, 40 rows).

