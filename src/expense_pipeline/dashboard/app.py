"""Streamlit dashboard for expense verification results."""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd
import streamlit as st

from expense_pipeline.dashboard import local_queries, queries
from expense_pipeline.decision_view import load_decision_process
from expense_pipeline.policy_index import PolicyCitation, find_policy_manual, load_policy_citations
from expense_pipeline.policy_parser import load_policy_rules_yaml, normalize_category
from expense_pipeline.schemas import Expense
from expense_pipeline.snowflake_conn import get_snowflake_connection, get_snowpark_session
from expense_pipeline.verifier import verify_compliance


def get_connection():
    """Get Snowflake connection, preferring Snowpark session if available."""
    session = get_snowpark_session()
    if session:
        return session.connection
    else:
        return get_snowflake_connection()


def _load_data_source():
    """Prefer Snowflake; fall back to local reports/*.csv."""
    try:
        conn = get_connection()
        run_ids = queries.get_run_ids(conn)
        if run_ids:
            return queries, conn, run_ids, "snowflake"
    except Exception:
        pass

    run_ids = local_queries.get_run_ids()
    return local_queries, None, run_ids, "local"


def main():
    """Main dashboard app."""
    st.set_page_config(page_title="Expense Verification Dashboard", layout="wide")
    st.title("Expense Verification Dashboard")

    data, conn, run_ids, source = _load_data_source()
    if source == "local":
        st.caption("Showing local reports (Snowflake is not configured).")

    if not run_ids:
        st.warning("No runs found. Run `expense-verify run sample_expenses.xlsx sample_policy_manual.docx` first.")
        return

    # Run selector
    selected_run_id = st.selectbox("Select a run:", run_ids)

    # Get run summary
    try:
        run_summary = data.get_run_summary(conn, selected_run_id)
    except Exception as e:
        st.error(f"Failed to load run summary: {e}")
        return

    if not run_summary:
        st.error("Run not found")
        return

    # Display run summary header
    st.subheader(f"Run: {selected_run_id}")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Processed", run_summary["rows_processed"])
    with col2:
        st.metric("Approved", run_summary["approved_count"])
    with col3:
        st.metric("Flagged", run_summary["flagged_count"])
    with col4:
        st.metric("Needs Review", run_summary["needs_review_count"])

    st.text(f"Run timestamp: {run_summary['run_timestamp']}")

    citations = _load_citations()
    policy_rules = _load_policy_rules()

    # Tabs for different views
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Expenses", "Department Breakdown", "Category Breakdown", "Needs Review", "About"]
    )

    with tab1:
        st.subheader("Expense Details")
        _render_expenses_tab(data, conn, selected_run_id, citations, policy_rules)

    with tab2:
        st.subheader("Department Breakdown")
        _render_department_tab(data, conn, selected_run_id)

    with tab3:
        st.subheader("Category Breakdown")
        _render_category_tab(data, conn, selected_run_id)

    with tab4:
        st.subheader("Expenses Requiring Human Review")
        _render_needs_review_tab(data, conn, selected_run_id, citations, policy_rules)

    with tab5:
        st.subheader("About This Dashboard")
        st.markdown("""
        This dashboard displays results from the expense verification pipeline.
        The pipeline verifies expenses against company spending policy using both
        LLM-assisted compliance checking and independent pure-code verification.

        **Status meanings:**
        - **Approved**: Both checker and verifier agree the expense complies with policy
        - **Flagged**: Both checker and verifier agree the expense violates policy
        - **Needs Review**: Disagreement between checker and verifier, requires human judgment
        """)


@st.cache_resource(show_spinner=False)
def _load_citations() -> dict[str, PolicyCitation]:
    manual = find_policy_manual()
    if not manual:
        return {}
    return load_policy_citations(str(manual))


@st.cache_resource(show_spinner=False)
def _load_policy_rules():
    candidates = [
        Path.cwd() / "src" / "expense_pipeline" / "data" / "policy_rules.yaml",
        Path(__file__).resolve().parents[1] / "data" / "policy_rules.yaml",
    ]
    for path in candidates:
        if path.is_file():
            return load_policy_rules_yaml(str(path))
    return None


def _render_expenses_tab(data, conn, run_id: str, citations, policy_rules):
    """Render the expenses table with filters and a selected-expense policy panel."""
    try:
        departments = data.get_unique_departments(conn, run_id)
        categories = data.get_unique_categories(conn, run_id)
        all_expenses = data.get_expenses_for_run(conn, run_id)
    except Exception as e:
        st.error(f"Failed to load filter options: {e}")
        return

    employees = sorted({exp["employee"] for exp in all_expenses if exp.get("employee")})

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        selected_employee = st.selectbox(
            "Pull up employee",
            ["All"] + employees,
            key="employee_filter",
        )
    with col2:
        selected_department = st.selectbox(
            "Filter by Department",
            ["All"] + departments,
            key="dept_filter",
        )
    with col3:
        selected_category = st.selectbox(
            "Filter by Category",
            ["All"] + categories,
            key="cat_filter",
        )
    with col4:
        selected_status = st.selectbox(
            "Filter by Status",
            ["All", "approved", "flagged", "needs_human_review"],
            key="status_filter",
        )

    try:
        expenses = [
            exp
            for exp in all_expenses
            if (selected_employee == "All" or exp["employee"] == selected_employee)
            and (selected_department == "All" or exp["department"] == selected_department)
            and (selected_category == "All" or exp["category"] == selected_category)
            and (selected_status == "All" or exp["final_status"] == selected_status)
        ]

        if not expenses:
            st.info("No expenses match the selected filters")
            return

        df = pd.DataFrame(expenses)
        display = df[
            [
                "report_id",
                "employee",
                "department",
                "category",
                "amount",
                "expense_date",
                "receipt_attached",
                "final_status",
            ]
        ].copy()
        display["receipt_attached"] = display["receipt_attached"].map({True: "Yes", False: "No"})
        display = display.rename(
            columns={
                "report_id": "Report ID",
                "employee": "Employee",
                "department": "Department",
                "category": "Category",
                "amount": "Amount",
                "expense_date": "Date",
                "receipt_attached": "Receipt",
                "final_status": "Status",
            }
        )

        st.caption("Select a row to open the matching policy citation.")
        event = st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="expense_table",
        )

        st.text(f"Total: ${display['Amount'].sum():.2f} across {len(display)} expenses")

        report_ids = [exp["report_id"] for exp in expenses]
        default_report = None
        selected_rows = getattr(getattr(event, "selection", None), "rows", None) or []
        if selected_rows:
            default_report = expenses[selected_rows[0]]["report_id"]

        picker_label = (
            f"Expense for {selected_employee}"
            if selected_employee != "All"
            else "Pull up expense"
        )
        selected_report = st.selectbox(
            picker_label,
            report_ids,
            index=report_ids.index(default_report) if default_report in report_ids else 0,
            key="expense_pick",
        )
        selected_expense = next(exp for exp in expenses if exp["report_id"] == selected_report)
        _render_expense_policy_panel(
            selected_expense, citations, policy_rules, run_id, panel_key="expenses"
        )

    except Exception as e:
        st.error(f"Failed to load expenses: {e}")


def _render_expense_policy_panel(
    expense: dict, citations, policy_rules, run_id: str, panel_key: str
) -> None:
    citation = citations.get(normalize_category(expense.get("category", "")))
    reasons = _expense_reasons(expense, policy_rules)

    st.markdown("---")
    title_col, action_col = st.columns([4, 1])
    with title_col:
        st.subheader(f"{expense['report_id']} — {expense['employee']}")
    with action_col:
        st.write("")
        if st.button(
            "View decision process",
            key=f"view_decision_{panel_key}_{run_id}_{expense['report_id']}",
        ):
            st.session_state["decision_focus"] = (run_id, expense["report_id"])

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Category", expense["category"])
    with col2:
        st.metric("Amount", f"${float(expense['amount']):.2f}")
    with col3:
        st.metric("Status", expense["final_status"])
    with col4:
        st.metric("Receipt", "Yes" if expense.get("receipt_attached") else "No")

    st.markdown(
        f"**Checker:** {expense.get('checker_verdict', 'n/a')} · "
        f"**Verifier:** {expense.get('verifier_verdict', 'n/a')}"
    )
    if reasons:
        st.markdown("**Why this result:**")
        for reason in reasons:
            st.write(f"- {reason}")

    if not citation:
        st.warning("No matching policy citation found for this category.")
        return

    st.markdown("### Corresponding policy")
    loc1, loc2, loc3 = st.columns(3)
    with loc1:
        st.metric("Section", citation.section)
    with loc2:
        st.metric("Page", citation.page)
    with loc3:
        st.metric("Paragraph", citation.paragraph)

    st.markdown(f"**Opening words:** _{citation.excerpt}_")

    if citation.table_excerpt:
        table_loc = f"page {citation.table_page}" if citation.table_page else "Section 4 table"
        if citation.table_row is not None:
            table_loc += f", table row {citation.table_row}"
        st.caption(f"Limits table ({table_loc}): {citation.table_excerpt}")

    if policy_rules:
        rule = next(
            (
                candidate
                for name, candidate in policy_rules.rules.items()
                if normalize_category(name) == normalize_category(expense.get("category", ""))
            ),
            None,
        )
        if rule:
            approval = (
                f" · Manager approval above ${rule.requires_manager_approval_above:.0f}"
                if rule.requires_manager_approval_above
                else ""
            )
            st.caption(
                f"Parsed limits: ${rule.daily_limit:.0f} daily · "
                f"receipt required above ${rule.receipt_required_above:.0f}{approval}"
            )

    focus = st.session_state.get("decision_focus")
    if focus == (run_id, expense["report_id"]):
        _render_decision_process(run_id, expense["report_id"])


def _split_stored_reasons(stored) -> list[str]:
    if isinstance(stored, list):
        return [str(part) for part in stored if str(part).strip()]
    return [part.strip() for part in str(stored).replace("|", ";").split(";") if part.strip()]


def _expense_reasons(expense: dict, policy_rules) -> list[str]:
    stored = expense.get("checker_reasons") or expense.get("verifier_reasons")
    if stored:
        return _split_stored_reasons(stored)
    if not policy_rules:
        return []
    reconstructed = Expense(
        report_id=str(expense.get("report_id", "")),
        employee=str(expense.get("employee", "")),
        department=str(expense.get("department", "")),
        date=str(expense.get("expense_date") or ""),
        category=str(expense.get("category", "")),
        amount=float(expense.get("amount") or 0),
        currency=str(expense.get("currency") or "USD"),
        receipt_attached=bool(expense.get("receipt_attached")),
    )
    verdict = verify_compliance([reconstructed], policy_rules).verdicts[0]
    return verdict.reasons


def _render_department_tab(data, conn, run_id: str):
    """Render department breakdown charts."""
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### Total Reimbursed by Department")
        try:
            dept_data = data.get_department_breakdown(conn, run_id)
            if dept_data:
                df = pd.DataFrame(dept_data)
                df.set_index("department", inplace=True)
                st.bar_chart(df)
            else:
                st.info("No data available")
        except Exception as e:
            st.error(f"Failed to load department breakdown: {e}")

    with col2:
        st.markdown("### Status Count by Department")
        try:
            dept_status = data.get_department_status_breakdown(conn, run_id)
            if dept_status:
                df = pd.DataFrame(dept_status)
                pivot_df = df.pivot(index="department", columns="status", values="count").fillna(0)
                st.bar_chart(pivot_df)
            else:
                st.info("No data available")
        except Exception as e:
            st.error(f"Failed to load department status breakdown: {e}")


def _render_category_tab(data, conn, run_id: str):
    """Render category breakdown chart."""
    st.markdown("### Total Spend by Category")
    try:
        cat_data = data.get_category_breakdown(conn, run_id)
        if cat_data:
            df = pd.DataFrame(cat_data)
            df.set_index("category", inplace=True)
            st.bar_chart(df[["total_amount"]])

            # Show category details as table
            st.markdown("### Category Details")
            detail_df = pd.DataFrame(cat_data)
            detail_df = detail_df.rename(
                columns={"category": "Category", "total_amount": "Total Amount", "count": "Count"}
            )
            st.dataframe(detail_df, width="stretch", hide_index=True)
        else:
            st.info("No data available")
    except Exception as e:
        st.error(f"Failed to load category breakdown: {e}")


def _render_needs_review_tab(data, conn, run_id: str, citations, policy_rules):
    """Render expenses needing human review."""
    try:
        needs_review = data.get_needs_review_expenses(conn, run_id)

        if not needs_review:
            st.success("No expenses require human review!")
            return

        st.warning(f"{len(needs_review)} expenses require human review")

        labels = [
            f"{exp['report_id']} · {exp['employee']} · {exp['category']} · ${float(exp['amount']):.2f}"
            for exp in needs_review
        ]
        selected_label = st.selectbox("Pull up an expense", labels, key="needs_review_pick")
        selected = needs_review[labels.index(selected_label)]
        _render_expense_policy_panel(
            selected, citations, policy_rules, run_id, panel_key="needs_review"
        )

    except Exception as e:
        st.error(f"Failed to load needs-review expenses: {e}")


def _render_decision_process(run_id: str, expense_id: str) -> None:
    """Show the stored decision log. This does not call a model or the verifier."""
    st.markdown("### Decision process")
    try:
        view = load_decision_process(run_id, expense_id)
    except Exception as exc:
        st.error(f"Could not read the decision log: {exc}")
        return

    if view["status"] == "legacy":
        st.warning(f"{view['headline']}: {view['detail']}")
    elif view["status"] == "incomplete":
        st.warning(f"{view['headline']}: {view['detail']}")
    else:
        st.caption(view["detail"])

    sections = view["sections"]
    outcome = sections.get("outcome") or {}
    if outcome:
        st.markdown(
            f"**Outcome:** {outcome.get('final_outcome')} · "
            f"decision `{outcome.get('decision_id')}` · "
            f"component `{outcome.get('responsible_component')}`"
        )

    evidence = sections.get("evidence") or {}
    if evidence:
        st.markdown("**Evidence**")
        st.write(
            {
                "source_row": evidence.get("source_row"),
                "input_facts": evidence.get("input_facts"),
            }
        )

    policy = sections.get("policy") or {}
    if policy:
        st.markdown("**Policy snapshot and applied clauses**")
        st.write(policy.get("snapshot"))
        for clause in policy.get("applied_clauses") or []:
            st.write(
                f"- {clause.get('clause')}: {clause.get('result')} "
                f"(threshold {clause.get('threshold')}, observed {clause.get('observed_value')})"
            )

    ai = sections.get("ai_assessment") or {}
    if ai:
        st.markdown(f"**{ai.get('label', 'AI assessment')}**")
        if ai.get("note"):
            st.caption(ai["note"])
        st.write(
            {
                "verdict": ai.get("verdict"),
                "stated_justification": ai.get("stated_justification"),
                "citations": ai.get("citations"),
                "citations_complete": ai.get("citations_complete"),
                "provider": ai.get("provider"),
                "model": ai.get("model"),
                "prompt_version": ai.get("prompt_version"),
                "settings": ai.get("settings"),
                "output_status": ai.get("output_status"),
            }
        )

    probability = sections.get("probability") or {}
    st.markdown(f"**{probability.get('label', 'Model output probability')}**")
    if probability.get("value") is None:
        st.write(f"Not available ({probability.get('reason') or 'missing'}).")
    else:
        st.write(
            f"{probability['value']:.6f} from {probability.get('provider')} / {probability.get('model')}"
        )
        if probability.get("logprob") is not None:
            st.caption(f"Label-token logprob: {probability['logprob']}")
        alternatives = probability.get("alternatives") or []
        if alternatives:
            st.write("Alternative tokens returned with that label:")
            st.write(alternatives)
    st.caption(probability.get("explanation", ""))

    verifier = sections.get("verifier")
    if verifier:
        st.markdown("**Verifier results**")
        st.write(
            {
                "verdict": verifier.get("verdict"),
                "reasons": verifier.get("reasons"),
                "observed_values": verifier.get("observed_values"),
                "checks": verifier.get("checks"),
            }
        )

    reconciliation = sections.get("reconciliation") or {}
    if reconciliation:
        st.markdown("**Reconciliation**")
        st.write(
            {
                "rule": reconciliation.get("rule"),
                "disagreement": reconciliation.get("disagreement"),
                "human_review": reconciliation.get("human_review"),
                "overrides": reconciliation.get("overrides"),
                "errors": reconciliation.get("errors"),
                "retries": reconciliation.get("retries"),
            }
        )

    st.markdown("**Event timeline**")
    timeline = sections.get("timeline") or []
    if not timeline:
        st.write("No stored events.")
        return
    for event in timeline:
        st.write(
            f"- {event.get('occurred_at')} · {event.get('component')} · {event.get('event_type')}"
        )
        payload = event.get("payload") or {}
        if event.get("event_type") == "ai_stated_output":
            st.caption("Model-stated justification, not an observed chain of reasoning.")
            st.write(payload.get("stated_justification"))
        elif payload:
            st.json(payload)


if __name__ == "__main__":
    main()
