"""
Tests for CSV upload validation.
Run from backend/: python -m pytest tests/ -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from validation import validate_csv

GW_HEADER = "settlement_id,order_ref,merchant,amount,date,narration\n"
LG_HEADER = "entry_id,order_ref,merchant,amount,date,narration\n"


def test_valid_gateway_csv():
    content = (GW_HEADER + "RZP1,ORD1,Acme,1000,2026-07-01,Order 1\n").encode()
    r = validate_csv(content, "gateway")
    assert r["valid"] is True
    assert r["row_count"] == 1


def test_missing_required_columns():
    content = b"foo,bar\n1,2\n"
    r = validate_csv(content, "gateway")
    assert r["valid"] is False
    assert "Missing required column" in r["errors"][0]


def test_empty_file():
    r = validate_csv(b"", "gateway")
    assert r["valid"] is False
    assert "empty" in r["errors"][0].lower()


def test_invalid_amount_reported_with_row_number():
    content = (GW_HEADER + "RZP1,ORD1,Acme,not_a_number,2026-07-01,Order 1\n").encode()
    r = validate_csv(content, "gateway")
    assert r["valid"] is False
    assert "Row 2" in r["errors"][0]
    assert "amount is invalid" in r["errors"][0]


def test_invalid_date_reported():
    content = (GW_HEADER + "RZP1,ORD1,Acme,1000,not-a-date,Order 1\n").encode()
    r = validate_csv(content, "gateway")
    assert r["valid"] is False
    assert "date is invalid" in r["errors"][0]


def test_missing_merchant_reported():
    content = (GW_HEADER + "RZP1,ORD1,,1000,2026-07-01,Order 1\n").encode()
    r = validate_csv(content, "gateway")
    assert r["valid"] is False
    assert "missing merchant" in r["errors"][0]


def test_duplicate_order_ref_is_advisory_not_blocking():
    content = (LG_HEADER +
               "LG1,ORD1,Acme,1000,2026-07-01,Order 1\n"
               "LG2,ORD1,Acme,1000,2026-07-01,Order 1 dup\n").encode()
    r = validate_csv(content, "ledger")
    assert r["valid"] is True  # duplicates don't block, just get flagged
    assert any("Duplicate" in e for e in r["errors"])


def test_malformed_csv_does_not_crash():
    content = b"this is not,, a csv at\x00all\xff\xfe"
    r = validate_csv(content, "gateway")
    assert isinstance(r, dict)
    assert "valid" in r


def test_multiple_row_errors_all_reported():
    content = (GW_HEADER +
               "RZP1,ORD1,Acme,bad_amount,2026-07-01,Order 1\n"
               "RZP2,ORD2,Acme,1000,bad_date,Order 2\n").encode()
    r = validate_csv(content, "gateway")
    assert r["valid"] is False
    assert len(r["errors"]) == 2
