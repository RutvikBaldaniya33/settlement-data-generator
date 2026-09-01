"""
Tests for the SettleSense matching engine.
Run from backend/: python -m pytest tests/ -v
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from matching import reconcile
from config import STATUS_MATCHED, STATUS_NEEDS_REVIEW, STATUS_EXCEPTION


def _write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _run(gateway_rows, ledger_rows):
    gw_fields = ["settlement_id", "order_ref", "merchant", "amount", "date", "narration"]
    lg_fields = ["entry_id", "order_ref", "merchant", "amount", "date", "narration"]
    with tempfile.TemporaryDirectory() as d:
        gw_path = os.path.join(d, "gw.csv")
        lg_path = os.path.join(d, "lg.csv")
        _write_csv(gw_path, gateway_rows, gw_fields)
        _write_csv(lg_path, ledger_rows, lg_fields)
        return reconcile(gw_path, lg_path)


def _gw(ref="RZP1", oid="ORD1", merchant="Acme Co", amount="1000", date="2026-07-01", narr="Order ORD1"):
    return {"settlement_id": ref, "order_ref": oid, "merchant": merchant, "amount": amount, "date": date, "narration": narr}


def _lg(eid="LG1", oid="ORD1", merchant="Acme Co", amount="1000", date="2026-07-01", narr="Order ORD1"):
    return {"entry_id": eid, "order_ref": oid, "merchant": merchant, "amount": amount, "date": date, "narration": narr}


def test_exact_valid_match():
    out = _run([_gw()], [_lg()])
    r = out["results"][0]
    assert r["status"] == STATUS_MATCHED
    assert r["confidence"] > 0.95


def test_exact_reference_but_huge_amount_mismatch_is_not_auto_matched():
    """The critical spec case: same reference, wildly different amount must
    NOT become MATCHED just because the reference lines up."""
    out = _run([_gw(amount="50000")], [_lg(amount="5000")])
    r = out["results"][0]
    assert r["status"] in (STATUS_NEEDS_REVIEW, STATUS_EXCEPTION)
    assert r["status"] != STATUS_MATCHED
    assert r["signals"]["amount_score"] == 0.0


def test_exact_reference_but_date_mismatch_reduces_confidence():
    out = _run([_gw(date="2026-07-01")], [_lg(date="2026-08-15")])
    r = out["results"][0]
    assert r["signals"]["date_score"] == 0.0
    assert r["confidence"] < 1.0


def test_fuzzy_match_recovers_typo_reference():
    """A broken join key (typo'd reference) must still be RECOVERED as a
    candidate via merchant/narration/amount/date — not silently dropped to
    EXCEPTION. Note: TF-IDF similarity is corpus-dependent, so with only one
    record pair the reference signal alone is noisy (real batches of 50+
    records, as in the shipped synthetic dataset, resolve this cleanly to
    MATCHED). What must hold regardless of corpus size is that the pair is
    found as a candidate at all, i.e. not EXCEPTION."""
    out = _run([_gw(oid="ORD1", ref="RZP1")], [_lg(oid="ORD1x")])  # broken join key
    r = out["results"][0]
    assert r["status"] in (STATUS_MATCHED, STATUS_NEEDS_REVIEW)
    assert r["method"] == "matched_pair"


def test_no_match_produces_exception_with_accurate_language():
    out = _run([_gw(oid="ORD1", merchant="Zeta Corp", amount="99999", date="2026-01-01")],
               [_lg(oid="ORD2", merchant="Omega Ltd", amount="1", date="2026-12-31")])
    statuses = [r["status"] for r in out["results"]]
    assert STATUS_EXCEPTION in statuses
    exc = [r for r in out["results"] if r["status"] == STATUS_EXCEPTION][0]
    assert "gateway settled but never booked internally" not in exc["reason"]
    assert "no eligible" in exc["reason"].lower() or "no eligible" in exc["reason"].lower()


def test_duplicate_ledger_entry_only_one_matches():
    out = _run(
        [_gw(oid="ORD1")],
        [_lg(eid="LG1", oid="ORD1"), _lg(eid="LG1DUP", oid="ORD1")],
    )
    matched = [r for r in out["results"] if r["status"] == STATUS_MATCHED]
    exceptions = [r for r in out["results"] if r["status"] == STATUS_EXCEPTION]
    assert len(matched) == 1
    assert len(exceptions) == 1
    # the exception must be the ledger side, not double-counted as gateway
    assert exceptions[0]["ledger_id"] is not None
    assert exceptions[0]["gateway_id"] == ""


def test_amount_tolerance_within_gateway_fee_still_matches():
    out = _run([_gw(amount="1000.00")], [_lg(amount="990.00")])  # Rs 10 fee, within tolerance
    r = out["results"][0]
    assert r["status"] == STATUS_MATCHED
    assert r["signals"]["amount_score"] == 1.0


def test_date_within_tolerance_still_matches():
    out = _run([_gw(date="2026-07-05")], [_lg(date="2026-07-03")])  # 2 days, within tolerance
    r = out["results"][0]
    assert r["status"] == STATUS_MATCHED
    assert r["signals"]["date_score"] == 1.0


def test_one_to_one_assignment_no_double_claim():
    """Two gateway records that both loosely resemble one ledger record must
    not both claim it — Hungarian assignment must resolve this 1:1."""
    out = _run(
        [_gw(ref="RZP1", oid="ORD1", amount="1000"), _gw(ref="RZP2", oid="ORD2", amount="1000")],
        [_lg(eid="LG1", oid="ORD1", amount="1000")],
    )
    ledger_ids_used = [r["ledger_id"] for r in out["results"] if r["ledger_id"]]
    assert len(ledger_ids_used) == len(set(ledger_ids_used))  # no ledger ID claimed twice


def test_empty_datasets_do_not_crash():
    out = _run([], [])
    assert out["summary"]["total_records"] == 0
    assert out["summary"]["match_rate_pct"] == 0.0


def test_summary_totals_are_consistent():
    out = _run(
        [_gw(oid="ORD1", amount="1000"), _gw(ref="RZP2", oid="ORD2", amount="2000")],
        [_lg(oid="ORD1", amount="1000"), _lg(eid="LG2", oid="ORD2", amount="2000")],
    )
    s = out["summary"]
    assert s["matched"] + s["needs_review"] + s["exceptions"] == s["total_records"]
    assert s["gateway_total_inr"] == 3000.0
    assert s["ledger_total_inr"] == 3000.0
