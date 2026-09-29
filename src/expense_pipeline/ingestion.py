"""Stage 1: Ingestion of expense spreadsheet."""

from pathlib import Path
from datetime import datetime

import pandas as pd

from .schemas import Expense, ExpenseSheet
from .gates import gate_ingestion, GateFailure


def ingest_expenses(spreadsheet_path: str) -> ExpenseSheet:
    """
    Read expense spreadsheet and return structured data.

    Gate re-opens the file to independently verify row count.
    """
    if not Path(spreadsheet_path).exists():
        raise FileNotFoundError(f"Spreadsheet not found: {spreadsheet_path}")

    try:
        df = pd.read_excel(spreadsheet_path, sheet_name="expenses")
    except Exception as e:
        raise ValueError(f"Failed to read spreadsheet: {e}")

    required_columns = {
        "report_id",
        "employee",
        "department",
        "date",
        "category",
        "amount",
        "currency",
        "receipt_attached",
    }
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    expenses = []
    for excel_index, (_, row) in enumerate(df.iterrows()):
        try:
            expense = Expense(
                report_id=str(row["report_id"]).strip(),
                employee=str(row["employee"]).strip(),
                department=str(row["department"]).strip(),
                date=str(row["date"]),
                category=str(row["category"]).strip(),
                amount=float(row["amount"]),
                currency=str(row["currency"]).strip(),
                receipt_attached=bool(row["receipt_attached"]),
                notes=str(row.get("notes", "")).strip() or None,
                source_row=excel_index + 2,
            )
            expenses.append(expense)
        except Exception as e:
            raise ValueError(f"Failed to parse row: {e}")

    sheet = ExpenseSheet(
        expenses=expenses, source_file=spreadsheet_path, row_count=len(expenses)
    )

    # Gate: re-open file and verify row count
    try:
        gate_ingestion(spreadsheet_path, len(expenses))
    except GateFailure as e:
        raise ValueError(f"Ingestion gate failed: {e}")

    return sheet
