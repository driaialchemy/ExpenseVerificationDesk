# Policy for Claude Working in This Repo

## Core Principles

1. **Gates are non-negotiable** â€” Never skip or weaken gate checks. Gates verify artifacts, not status strings or LLM text.
2. **Verifier independence** â€” Stage 4 (verifier.py) must remain pure code with zero LLM/network calls. It re-derives verdicts independently.
3. **No secrets in git** â€” Environment variables only for credentials. Never commit `.env`, API keys, or passwords.

## What Claude Should Do

- **Write tests** for each stage and gate
- **Implement missing stages** following the build order in the brief
- **Fix bugs** that violate gates or verifier independence
- **Document** why a decision was made (constraints, tradeoffs)
- **Run tests locally** before claiming work is done

## What Claude Should NOT Do

- Skip gates or replace them with `status == "success"` checks
- Read LLM output in the verifier (or anywhere else it should be independent)
- Commit credentials or .env files
- Hardcode Snowflake connection details
- Mix data flow from different stages improperly (e.g., verifier reading checker output)
- Add features beyond the build brief without asking

## Testing Standards

- Every stage gets a test file (`test_*.py`)
- Gates get tests for both passing and failing cases
- Mocks are used for LLM and Snowflake calls in unit tests
- End-to-end tests use real temp files but no real Snowflake

## Code Style

- Keep functions focused and small
- Use type hints throughout
- Default to no comments; only add if the why is non-obvious
- Preserve the audit trail at every stage

## Questions for Claude

Before implementing a change, ask if:

1. Does this violate a gate check?
2. Would this make the verifier depend on external state?
3. Could this leak a secret?
4. Is this in the build brief, or am I adding scope?

If uncertain, ask the user before proceeding.

## Audit Remediation Notes

- Audit path: $AuditFile
- Risk level from audit: $risk.
- Addressed audit issues: Spreadsheet/data export files found: sample_data/sample_expenses.xlsx; External AI API usage: anthropic — API key management required.; Credential pattern indicators in code: api_key; Risk level is HIGH, expected LOW.; Dependency manifests present: pyproject.toml; Test suite present — actively developed project.; Missing AGENTS.md — no machine-readable safety policy.


## AI Provider Boundary

AI providers detected by audit: anthropic. Use providers only for the repository's documented workflow. API keys must be supplied through environment variables or managed platform secrets, never committed or echoed. Do not add redundant providers without human approval.


## Data Export Boundary

Spreadsheet/data export files detected by audit: sample_data/sample_expenses.xlsx. Keep generated exports gitignored unless they are documented fixtures. Do not replace synthetic/sample fixtures with real operational data.


## Human Review Required

This audit classified the repo as HIGH risk. Agents may draft governance/documentation updates, but credential, provider, database, deployment, and data-export remediation requires human review before code or data changes.

