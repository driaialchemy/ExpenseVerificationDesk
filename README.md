# Expense Verification Pipeline

A production-ready, gated multi-agent pipeline that verifies expense reports against company spending policy, loads verified results to Snowflake, and exposes a dual-mode Streamlit dashboard.

**Key Features:**
- 🔒 **Gated verification** — every stage checked against artifacts, never status flags
- 🔍 **Independent verification** — pure-code verifier that re-derives verdicts without reading checker output
- 📊 **Snowflake integration** — independent row-count verification on database load
- 📈 **Dual-mode dashboard** — runs standalone or native in Snowflake
- 🧪 **Comprehensive tests** — 31 tests covering all stages and gates (100% passing)
- 📝 **Full audit trail** — JSON logging of every decision and gate result

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/driaialchemy/expenseverificationpipeline.git
cd expenseverificationpipeline

# Install package and dev dependencies
pip install -e .
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest tests/ -v
```

All 31 tests pass with 100% success rate.

### Run the Pipeline

```bash
# Without Snowflake (local reports only)
expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx

# With Snowflake load (requires credentials)
export ANTHROPIC_API_KEY=sk-...
export SNOWFLAKE_ACCOUNT=xy12345.us-east-1
export SNOWFLAKE_USER=your_user
export SNOWFLAKE_PASSWORD=your_password
export SNOWFLAKE_WAREHOUSE=COMPUTE_WH
export SNOWFLAKE_DATABASE=your_db
export SNOWFLAKE_SCHEMA=your_schema
export SNOWFLAKE_ROLE=your_role

expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx --load-to-snowflake
```

### View Dashboard

```bash
# Standalone Streamlit
streamlit run src/expense_pipeline/dashboard/app.py

# Or deploy to Snowflake (see "Deploying the Dashboard" section below)
```

## Architecture

### Pipeline Stages

```
Excel Spreadsheet
       ↓
[Stage 1: Ingestion] → ExpenseSheet
       ↓
Word Policy Manual
       ↓
[Stage 2: Policy Parser] → PolicyRules (YAML)
       ↓
[Stage 3: Checker (LLM)] → checker_verdicts
[Stage 4: Verifier (Pure Code)] → verifier_verdicts
       ↓
[Stage 5: Approver] → approved_expenses (approved/flagged/needs_review)
       ↓
[Stage 6: Snowflake Loader] → EXPENSE_VERDICTS + RUN_SUMMARY
```

### Gate Checks

Every stage is protected by a gate that verifies actual data:

| Stage | Gate | Verification |
|-------|------|--------------|
| 1 | `gate_ingestion()` | File re-opened, row count matches |
| 2 | `gate_policy_parser()` | All expense categories have policy rules |
| 3 | `gate_checker()` | Row-count parity, every row has verdict |
| 4 | `gate_verifier()` | Row-count parity, every row has verdict |
| 5 | `gate_approver()` | All rows classified (approved/flagged/needs_review) |
| 6 | `gate_snowflake_load_verified()` | Independent `SELECT COUNT(*)` confirms row count |

**Core principle:** Gates check artifacts and re-computed values, never LLM output text or status flags.

### Data Models

```python
Expense
├── report_id, employee, department, date
├── category, amount, currency
├── receipt_attached, notes

ExpenseVerdict
├── report_id, verdict (approved|flagged)
├── reasons: list[str]
└── rule_citations: list[str]

ApprovedExpense
├── expense: Expense
├── checker_verdict: ExpenseVerdict
├── verifier_verdict: ExpenseVerdict
└── final_status: str (approved|flagged|needs_human_review)
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
# Snowflake Connection (required only if using Stage 6)
SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=your_user
SNOWFLAKE_PASSWORD=your_password  # OR SNOWFLAKE_PRIVATE_KEY_PATH for key-pair auth
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_DATABASE=your_database
SNOWFLAKE_SCHEMA=your_schema
SNOWFLAKE_ROLE=your_role

# Anthropic API (required for Stage 3: Checker)
ANTHROPIC_API_KEY=sk-...
```

### Policy Rules

Policy rules are extracted from the Word document during Stage 2 and saved to `data/policy_rules.yaml`:

```yaml
meals:
  daily_limit: 75.0
  receipt_required_above: 30.0

travel:
  daily_limit: 500.0
  receipt_required_above: 150.0

software:
  daily_limit: 1000.0
  receipt_required_above: 500.0
  requires_manager_approval_above: 750.0
```

## Project Structure

```
expenseverificationpipeline/
├── src/expense_pipeline/
│   ├── __init__.py
│   ├── cli.py                         # CLI entry point
│   ├── schemas.py                     # 15 dataclasses for all artifacts
│   ├── ingestion.py                   # Stage 1: Parse Excel
│   ├── policy_parser.py               # Stage 2: Parse Word policy
│   ├── checker.py                     # Stage 3: LLM compliance check
│   ├── verifier.py                    # Stage 4: Pure code verification
│   ├── approver.py                    # Stage 5: Reconcile verdicts
│   ├── snowflake_loader.py            # Stage 6: Load to Snowflake
│   ├── gates.py                       # 6 gate checks + logging
│   ├── audit.py                       # JSON audit trail
│   ├── snowflake_conn.py              # DB connection (env vars only)
│   └── dashboard/
│       ├── app.py                     # Streamlit dashboard
│       └── queries.py                 # 8 named SQL queries
├── tests/                             # 31 tests (100% passing)
│   ├── test_ingestion.py
│   ├── test_policy_parser.py
│   ├── test_checker.py
│   ├── test_verifier.py
│   ├── test_gates.py
│   ├── test_snowflake_loader.py
│   └── test_end_to_end.py
├── sql/
│   └── 001_create_tables.sql         # Snowflake schema (EXPENSE_VERDICTS + RUN_SUMMARY)
├── data/
│   └── policy_rules.yaml             # Parsed policy rules
├── audit/                            # Audit JSON output (gitignored)
├── reports/                          # CSV reports (gitignored)
├── sample_data/sample_expenses.xlsx              # Test data
├── sample_policy_manual.docx         # Test data
├── pyproject.toml                    # Package config
├── .gitignore
├── .env.example                      # Credential template
├── CLAUDE.md                         # AI agent policy
└── README.md                         # This file
```

## Usage Examples

### Basic Pipeline Run

```bash
expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx
```

Output:
```
Starting expense verification run: 40125926
Stage 1: Ingesting expenses...
  [OK] Loaded 40 expenses
Stage 2: Parsing policy manual...
  [OK] Parsed 6 policy categories
Stage 3: Running compliance checks...
  [OK] Generated 40 verdicts
Stage 4: Running independent verification...
  [OK] Verified 40 verdicts
Stage 5: Finalizing approvals...
  [OK] Approved 32 expenses
  [OK] Flagged 5 expenses
  [OK] 3 need human review
Stage 6: Skipped (use --load-to-snowflake to enable)
Writing reports...

[OK] Pipeline complete: 40125926
  Audit trail: audit/40125926.json
  Reports: reports/
```

### With Snowflake

```bash
expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx --load-to-snowflake
```

Stage 6 loads to Snowflake and independently verifies the row count before marking success.

### Dashboard

Access the dashboard to view results:

```bash
streamlit run src/expense_pipeline/dashboard/app.py
```

Features:
- **Run Selector** — choose which run to view
- **Expense Table** — filterable by department, category, status; sortable by amount
- **Department Breakdown** — bar charts of total amount and status distribution
- **Category Breakdown** — pie/bar chart of spend by category
- **Needs Review Section** — visually separated table of flagged expenses
- **Run Summary** — metrics and timestamps

## Deploying the Dashboard

### Option 1: Streamlit-in-Snowflake (Recommended)

1. In Snowsight, go to **Streamlit apps**
2. Click "Create app"
3. Point to `src/expense_pipeline/dashboard/app.py`
4. Deploy

The app auto-detects the Snowpark session and queries Snowflake directly with no external credentials needed.

### Option 2: Standalone Streamlit (Any URL)

```bash
# Local development
streamlit run src/expense_pipeline/dashboard/app.py

# Deploy to Streamlit Community Cloud
# 1. Push repo to GitHub
# 2. Go to https://share.streamlit.io/
# 3. Connect repo and specify src/expense_pipeline/dashboard/app.py
# 4. Set env vars in "Advanced settings"
```

The app auto-detects if a Snowpark session is available. If not, it uses `snowflake_conn.py` to connect via env vars.

## Testing

### Run All Tests

```bash
pytest tests/ -v
```

### Run Specific Test File

```bash
pytest tests/test_verifier.py -v
```

### Coverage Report

```bash
pytest --cov=src/expense_pipeline tests/
```

### Test Structure

- **Unit tests** mock all external dependencies (LLM, Snowflake)
- **Integration tests** use real temp files (Excel, Word, YAML)
- **End-to-end tests** cover full local pipeline (Stages 1-5)
- **Gate tests** verify both passing and failing scenarios

All 31 tests pass with no external credentials required.

## Key Design Decisions

### 1. Gates on Artifacts, Not Status Flags

Every gate re-computes or re-reads the actual data:

```python
# WRONG - trusts the function's return value
def load_to_snowflake(run_id, expenses):
    cursor.execute("INSERT INTO EXPENSE_VERDICTS...")
    return True  # ← Gate would trust this
    
# RIGHT - gate independently verifies
cursor.execute("SELECT COUNT(*) FROM EXPENSE_VERDICTS WHERE run_id = %s")
actual_count = cursor.fetchone()[0]
if actual_count != expected_count:
    raise GateFailure(...)
```

### 2. Pure-Code Verifier

Stage 4 is intentionally pure Python with zero LLM/network calls:

```python
def verify_compliance(expenses, policy_rules):
    # Uses only raw spreadsheet + raw policy YAML
    # Never reads Checker output
    # Independently re-derives every verdict
    return verdicts
```

This ensures independent verification even if the LLM is wrong or misconfigured.

### 3. Additive Snowflake Writes

Inserts are tagged by `run_id` and never destructive:

```python
cursor.execute("""
    INSERT INTO EXPENSE_VERDICTS (run_id, ...) 
    VALUES (%s, ...)
""", (run_id, ...))
```

History is preserved. Cleanup requires explicit commands outside the pipeline.

### 4. Environment-Only Credentials

No hardcoded secrets or config files:

```python
SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT")
# Never: SNOWFLAKE_ACCOUNT = "xy12345.us-east-1"
```

`.env` is gitignored; `.env.example` has placeholders only.

## Troubleshooting

### "ModuleNotFoundError: No module named 'anthropic'"

```bash
pip install -e .
```

### "Could not resolve authentication method"

The Anthropic API key is missing. Either:
- Set `ANTHROPIC_API_KEY` environment variable (required for Stage 3)
- Run without Stage 3 (pipeline stops after Stage 2 and writes local reports)

### Stage 3 "Illegal header value" / Connection error with API key in the message

`ANTHROPIC_API_KEY` has a leading or trailing space (or quotes). HTTP headers cannot contain that whitespace. The checker now strips the key automatically. Rotate the key if it was printed in a terminal or log.

### Stage 3 "Connection error."

The Anthropic SDK hides the real TLS/network failure. The pipeline now retries over IPv4 and prints the underlying error. If `api.anthropic.com` is blocked (firewall, proxy, or SSL inspection), finish the run with the independent verifier:

```bash
expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx --skip-checker
```

For a corporate proxy:

```bash
# PowerShell
$env:HTTPS_PROXY="http://proxy.example.com:8080"
```

### Stage 3 404 / "model is not available"

The API key is valid, but the model ID is retired or not enabled for that key. The checker defaults to `claude-sonnet-5` and will try another current model if the requested one 404s.

```bash
# PowerShell
$env:ANTHROPIC_MODEL="claude-sonnet-5"
expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx

# or pass it on the command line
expense-verify run sample_data/sample_expenses.xlsx sample_policy_manual.docx --model claude-haiku-4-5
```

Current IDs: `claude-sonnet-5`, `claude-haiku-4-5`, `claude-opus-5`. Do not use `claude-3-5-sonnet-20241022` or `claude-opus-4-1`.

### "GateFailure: Row count mismatch in Snowflake"

The database insert failed partially. Check:
- Snowflake table exists (run `sql/001_create_tables.sql`)
- No concurrent writes to the same `run_id`
- Network connection is stable

### Policy rules not extracted

The Word document structure doesn't match expected format. Ensure Section 4 contains a table with columns: Category, Daily Limit, Receipt Required Above, Manager Approval Above.

## Contributing

This codebase follows the principles in [CLAUDE.md](CLAUDE.md):
- Gates are non-negotiable
- Verifier must remain pure code
- No secrets in git
- Every stage has tests

Before contributing, read CLAUDE.md and run `pytest tests/` to verify your changes don't break gates or verifier independence.

## License

Internal use only. Proprietary to Meridian Fielding Group.

## Support

For issues or questions:
1. Check [CLAUDE.md](CLAUDE.md) for design principles
2. Read the docstrings in [src/expense_pipeline](src/expense_pipeline/)
3. Review test cases in [tests/](tests/)
4. Open an issue on GitHub

## Changelog

### v0.1.0 (Initial Release)

- Complete 6-stage pipeline with gates
- LLM-assisted and pure-code verification
- Snowflake integration with independent verification
- Streamlit dashboard (standalone + Snowflake-native)
- CLI with audit trail and CSV reports
- 31 passing tests
- Full documentation

---

**Built with:**
- Python 3.11+
- Anthropic Claude API
- Snowflake Connector + Snowpark
- Streamlit
- Pandas + OpenPyXL + Python-DOCX + PyYAML

**Status:** Production-ready
