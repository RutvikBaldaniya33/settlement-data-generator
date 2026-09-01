"""
CSV validation for uploaded gateway/ledger files.

Runs before reconciliation ever touches the data. Never raises on bad rows —
collects every problem and returns a structured report so the frontend can
show "N errors found" with row-level detail, per the buildathon UX bar.
"""
import csv
import io
from datetime import datetime

REQUIRED_GATEWAY_COLUMNS = ["settlement_id", "order_ref", "merchant", "amount", "date", "narration"]
REQUIRED_LEDGER_COLUMNS = ["entry_id", "order_ref", "merchant", "amount", "date", "narration"]


def _parse_amount(value):
    try:
        f = float(value)
        return f if f >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def validate_csv(file_bytes: bytes, kind: str) -> dict:
    """kind is 'gateway' or 'ledger'. Returns {valid, errors, row_count, preview}."""
    required = REQUIRED_GATEWAY_COLUMNS if kind == "gateway" else REQUIRED_LEDGER_COLUMNS
    id_field = "settlement_id" if kind == "gateway" else "entry_id"
    errors = []

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"valid": False, "errors": ["File is not valid UTF-8 text."], "row_count": 0, "preview": []}

    if not text.strip():
        return {"valid": False, "errors": ["File is empty."], "row_count": 0, "preview": []}

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {"valid": False, "errors": ["Could not read a header row."], "row_count": 0, "preview": []}

    missing_cols = [c for c in required if c not in reader.fieldnames]
    if missing_cols:
        return {
            "valid": False,
            "errors": [f"Missing required column(s): {', '.join(missing_cols)}"],
            "row_count": 0, "preview": [],
        }

    rows = []
    seen_refs = {}
    row_num = 1  # header is row 1, data starts at row 2
    for row in reader:
        row_num += 1
        row_errors = []

        rid = (row.get(id_field) or "").strip()
        if not rid:
            row_errors.append(f"missing {id_field}")

        order_ref = (row.get("order_ref") or "").strip()
        if not order_ref:
            row_errors.append("missing order_ref")

        merchant = (row.get("merchant") or "").strip()
        if not merchant:
            row_errors.append("missing merchant")

        amount = _parse_amount(row.get("amount"))
        if amount is None:
            row_errors.append("amount is invalid")

        if not _parse_date(row.get("date")):
            row_errors.append("date is invalid")

        if row_errors:
            errors.append(f"Row {row_num}: {', '.join(row_errors)}")
        else:
            rows.append(row)
            seen_refs.setdefault(order_ref, []).append(row_num)

    duplicate_refs = {ref: nums for ref, nums in seen_refs.items() if len(nums) > 1}
    if duplicate_refs:
        for ref, nums in list(duplicate_refs.items())[:5]:
            errors.append(f"Duplicate order_ref '{ref}' at rows {nums} — allowed, but will need "
                           f"disambiguation (e.g. a genuine duplicate booking).")

    return {
        "valid": len([e for e in errors if not e.startswith("Duplicate")]) == 0,
        "errors": errors,
        "row_count": len(rows),
        "preview": rows[:5],
        "columns": reader.fieldnames,
    }
