# Expense Verification Pipeline - Build Summary

## Prototype status

The pipeline, decision log, and dashboard view are implemented and covered by local tests. This is not a production certification, and the tests are not an accuracy measurement.

### Repository Structure

```
expense-verification-pipeline/
├── src/expense_pipeline/
│   ├── __init__.py
│   ├── cli.py                    # CLI entry point
│   ├── schemas.py                # Data models (15 dataclasses)
│   ├── ingestion.py              # Stage 1: Excel parsing
│   ├── policy_parser.py          # Stage 2: DOCX policy extraction
│   ├── checker.py                # Stage 3: LLM-assisted checking
│   ├── verifier.py               # Stage 4: Pure code verification
│   ├── approver.py               # Stage 5: Verdict reconciliation
│   ├── snowflake_loader.py       # Stage 6: Snowflake write
│   ├── gates.py                  # Gate checks (6 gates)
│   ├── audit.py                  # Audit trail logging
│   ├── snowflake_conn.py         # DB connection
│   └── dashboard/
│       ├── app.py                # Streamlit dashboard
│       └── queries.py            # SQL queries (8 queries)
├── tests/
│   ├── test_ingestion.py         # 4 tests
│   ├── test_policy_parser.py     # 4 tests
│   ├── test_checker.py           # 4 tests
│   ├── test_verifier.py          # 6 tests
│   ├── test_gates.py             # 4 tests
│   ├── test_snowflake_loader.py  # 6 tests
│   └── test_end_to_end.py        # 3 tests
├── sql/
│   └── 001_create_tables.sql     # Schema DDL
├── pyproject.toml                # Package config
├── .gitignore                    # Git ignore rules
├── .env.example                  # Credential template
├── README.md                      # Full documentation
└── CLAUDE.md                      # AI agent policy
```

### Test Results

Local `pytest` covers ingestion, policy parsing, the checker (including the configured model id), the verifier, gates, the Snowflake loader with mocks, end-to-end temp files, the decision log, OpenAI logprob handling with a fake client, and dashboard retrieval. Re-run `pytest tests/ -q` for the current count. No live model or Snowflake call is part of that suite.

### Verified Features

#### Core Pipeline
- [x] Stage 1: Excel ingestion with department column
- [x] Stage 2: Policy manual parsing (handles real DOCX)
- [x] Stage 3: LLM-assisted compliance checking
- [x] Stage 4: Pure code independent verification
- [x] Stage 5: Verdict reconciliation and approval
- [x] Stage 6: Snowflake load with independent verification

#### Gate Checks
- [x] Ingestion gate: file re-open + row count
- [x] Policy parser gate: category coverage check
- [x] Checker gate: row count parity + complete verdicts
- [x] Verifier gate: row count parity + complete verdicts
- [x] Approver gate: all rows classified
- [x] Snowflake gate: independent COUNT(*) verification

#### Dashboard
- [x] Streamlit UI (works standalone or in Snowflake)
- [x] Run selection and summary metrics
- [x] Expense table with filters (department, category, status)
- [x] Department breakdown charts
- [x] Category breakdown charts
- [x] Needs-review callout section
- [x] View decision process reads the local decision log and does not call a model
- [x] Incomplete and legacy records are labeled
- [x] SQL query abstraction layer

#### CLI
- [x] `expense-verify run <spreadsheet> <policy> [--load-to-snowflake]`
- [x] Audit JSON trail per run
- [x] CSV report output
- [x] Stage-by-stage logging

#### Decision log
- [x] Versioned SQLite record per expense: ids, timestamps, source row, input and policy snapshots, applied clauses, model-stated output, verifier checks, disagreement, reconciliation rule, errors, and retries
- [x] Append-only events; a later recorded human review keeps the earlier version
- [x] Mandatory audit-write failure blocks finalization and Snowflake loading
- [x] CSV export keeps the original columns and appends reasons plus the reconciliation rule
- [x] `audit/`, `reports/`, and SQLite files are gitignored

#### OpenAI observation mode
- [x] Anthropic remains the default checker
- [x] OpenAI mode classifies with the labels `approved` and `flagged`, then requests a separate explanation
- [x] Label-token logprobs are stored only for that model and converted with `exp(logprob)`
- [x] Invalid labels, refusals, and missing scores are explicit; missing probabilities stay null
- [x] Probabilities do not change the approver

#### Daily limit
- [x] Same employee, calendar day, category, and currency are summed
- [x] Different currencies are not converted or added together
- [x] Receipt and manager-approval checks stay per line
- [x] Applied clauses are cited on pass and fail

#### Safety & Design
- [x] No secrets in git (.env gitignored)
- [x] Verifier is pure code (no LLM/network)
- [x] Gates check artifacts, not status flags
- [x] Snowflake writes are additive (tagged by run_id)
- [x] Model id from `--model` or `ANTHROPIC_MODEL` is sent to the checker
- [x] Audit write failures are raised instead of printed and ignored

### Real Data Tested

✓ **Pipeline successfully ran against sample data:**
- Loaded 40 expenses from sample_expenses.xlsx
- Parsed 6 policy categories from sample_policy_manual.docx
- Successfully completed Stages 1-2

(Stages 3+ require ANTHROPIC_API_KEY environment variable)

### Quick Start

```bash
# Install
pip install -e .
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run pipeline (stages 1-5 only)
expense-verify run sample_expenses.xlsx sample_policy_manual.docx

# Run with Snowflake (requires credentials)
export ANTHROPIC_API_KEY=sk-...
export SNOWFLAKE_ACCOUNT=xy12345.us-east-1
# ... set other env vars from .env.example
expense-verify run sample_expenses.xlsx sample_policy_manual.docx --load-to-snowflake

# Start dashboard
streamlit run src/expense_pipeline/dashboard/app.py
```

### Build Notes

1. **Dependencies**: All required packages specified in pyproject.toml
2. **Python 3.11+**: Requires Python 3.11 or later
3. **Character Encoding**: CLI uses ASCII-safe output for Windows compatibility
4. **Policy Parser**: Improved to handle real Word documents with flexible table parsing
5. **All tests mocked**: Snowflake and Anthropic calls are mocked in unit tests

## What Was Built

This prototype demonstrates:
- Multi-stage expense checks with an independent verifier
- Gate checks on artifacts
- A versioned local decision log
- A dashboard that can show the stored decision process
- An optional OpenAI label-probability mode that does not change approvals

It is not a claim that the prototype is ready for production, and it is not a measurement of how often the verdicts would match a human reviewer.
