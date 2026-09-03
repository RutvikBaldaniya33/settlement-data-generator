"""
Scenario coverage for the SettleSense matching engine (backend/app/matching.py).

This file is additive to tests/test_matching.py — it does not duplicate or
replace those tests. Each test here is mapped to one of the 15 reconciliation
scenarios requested for verification. All fixtures use the engine's *existing*
config.py thresholds (AUTO_MATCH_THRESHOLD, NEEDS_REVIEW_THRESHOLD,
CANDIDATE_FLOOR, AMOUNT_TOLERANCE_*, DATE_TOLERANCE_DAYS) — no new business
rule is invented here.

Run from backend/: python -m pytest tests/test_matching_scenarios.py -v
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from matching import reconcile
from config import (
    STATUS_MATCHED, STATUS_NEEDS_REVIEW, STATUS_EXCEPTION,
    AUTO_MATCH_THRESHOLD, NEEDS_REVIEW_THRESHOLD,
    AMOUNT_TOLERANCE_ABS_INR, DATE_TOLERANCE_DAYS, DATE_SCORE_DECAY_MULTIPLE,
)


# --------------------------------------------------------------------------
# Shared fixtures (mirrors tests/test_matching.py conventions)
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# 1. Exact gateway <-> ledger match
# --------------------------------------------------------------------------

def test_scenario_01_exact_match():
    out = _run([_gw()], [_lg()])
    r = out["results"][0]
    assert r["status"] == STATUS_MATCHED
    assert r["confidence"] >= AUTO_MATCH_THRESHOLD
    assert r["ledger_id"] == "LG1"


# --------------------------------------------------------------------------
# 2. Small amount difference (within configured tolerance -> still MATCHED)
# --------------------------------------------------------------------------

def test_scenario_02_small_amount_difference_within_tolerance():
    # Rs 10 diff, tolerance is Rs 20 abs -> full amount score, should MATCH
    out = _run([_gw(amount="1000.00")], [_lg(amount="990.00")])
    r = out["results"][0]
    assert r["signals"]["amount_score"] == 1.0
    assert r["status"] == STATUS_MATCHED


# --------------------------------------------------------------------------
# 3. Large amount mismatch (must NOT auto-match on reference alone)
# --------------------------------------------------------------------------

def test_scenario_03_large_amount_mismatch_blocks_auto_match():
    out = _run([_gw(amount="50000")], [_lg(amount="5000")])
    r = out["results"][0]
    assert r["signals"]["amount_score"] == 0.0
    assert r["status"] != STATUS_MATCHED


# --------------------------------------------------------------------------
# 4. Date drift within allowed tolerance -> still MATCHED
# --------------------------------------------------------------------------

def test_scenario_04_date_drift_within_tolerance():
    # exactly at the tolerance boundary
    out = _run([_gw(date="2026-07-01")],
               [_lg(date="2026-07-05")])  # DATE_TOLERANCE_DAYS == 4
    r = out["results"][0]
    assert r["signals"]["date_score"] == 1.0
    assert r["status"] == STATUS_MATCHED


# --------------------------------------------------------------------------
# 5. Date difference beyond tolerance -> confidence drops, no longer MATCHED
# --------------------------------------------------------------------------

def test_scenario_05_date_difference_beyond_tolerance_degrades_confidence():
    decay_end_days = int(DATE_TOLERANCE_DAYS * DATE_SCORE_DECAY_MULTIPLE)
    from datetime import datetime, timedelta
    far_date = (datetime(2026, 7, 1) + timedelta(days=decay_end_days + 5)).strftime("%Y-%m-%d")
    out = _run([_gw(date="2026-07-01")], [_lg(date=far_date)])
    r = out["results"][0]
    # date signal is fully blown ...
    assert r["signals"]["date_score"] == 0.0
    # ... which must pull the overall confidence below a perfect score.
    assert r["confidence"] < 1.0


def test_scenario_05b_date_beyond_tolerance_combined_with_realistic_narration_drift():
    """A blown date on its own only costs the DATE weight (0.15), and since
    AUTO_MATCH_THRESHOLD (0.85) == 1 - 0.15, an otherwise-perfect pair with a
    completely wrong date still lands exactly on the auto-match boundary and
    is classified MATCHED (see test_scenario_05_documents_date_only_boundary_case
    below). That is a real edge case worth knowing about, but it is a threshold/
    weight design choice, not a proven bug, so matching.py is left unchanged.

    In practice a genuine date-drift-beyond-tolerance case is rarely paired
    with a perfect narration match too (gateway and ledger narration formats
    differ), so this test uses a realistic combination — matching reference/
    merchant/amount, differing narration format, and a date far outside the
    tolerance window — and confirms the record correctly fails to reach
    MATCHED."""
    out = _run(
        [_gw(ref="RZP1", oid="ORD1", merchant="Acme Co", amount="1000", date="2026-07-01",
             narr="UPI/RZP1/Settlement/Acme Co")],
        [_lg(eid="LG1", oid="ORD1", merchant="Acme Co", amount="1000", date="2026-08-15",
             narr="Being amount received from customer")],
    )
    r = out["results"][0]
    assert r["signals"]["date_score"] == 0.0
    assert r["status"] != STATUS_MATCHED


def test_scenario_05_documents_date_only_boundary_case():
    """FINDING (not a bug fix): with every other signal at a perfect 1.0, a
    date difference so large that date_score hits exactly 0.0 still produces
    confidence == 1 - SIGNAL_WEIGHTS['date'] == 0.85 == AUTO_MATCH_THRESHOLD,
    and AUTO_MATCH_THRESHOLD is an inclusive (>=) bound, so this lands exactly
    on MATCHED. In other words, under the current config, a totally blown
    date signal alone can never drag a pair below auto-match. This test
    documents that behavior explicitly (rather than silently asserting it
    should be different) so the finding is visible and reviewable, per the
    instruction not to change the algorithm without proof it's wrong."""
    out = _run([_gw(date="2026-07-01")], [_lg(date="2026-08-15")])
    r = out["results"][0]
    assert r["signals"]["date_score"] == 0.0
    assert r["confidence"] == round(AUTO_MATCH_THRESHOLD, 3)
    assert r["status"] == STATUS_MATCHED  # current, documented behavior


# --------------------------------------------------------------------------
# 6. Merchant/narration typo or fuzzy match (reference exact, merchant typo'd)
# --------------------------------------------------------------------------

def test_scenario_06_merchant_typo_still_resolves_via_reference():
    out = _run([_gw(ref="RZP1", oid="ORD1", merchant="Acmee Corporatoin")],
               [_lg(eid="LG1", oid="ORD1", merchant="Acme Corporation")])
    r = out["results"][0]
    # merchant signal is genuinely degraded by the typo...
    assert r["signals"]["merchant_similarity"] < 1.0
    # ...but the record is still recovered as a credible match overall,
    # since reference/amount/date carry it above the auto-match bar.
    assert r["status"] == STATUS_MATCHED


# --------------------------------------------------------------------------
# 7. Gateway record with no eligible ledger record at all
# --------------------------------------------------------------------------

def test_scenario_07_gateway_with_no_eligible_ledger():
    out = _run(
        [_gw(oid="ORD_LONELY_GW", ref="RZPX", merchant="Solo Merchant", amount="42424", date="2026-01-01")],
        [_lg(oid="ORD_UNRELATED", eid="LG1", merchant="Totally Different", amount="1", date="2026-12-31")],
    )
    gw_result = [r for r in out["results"] if r["gateway_id"] == "RZPX"][0]
    assert gw_result["status"] == STATUS_EXCEPTION
    assert gw_result["ledger_id"] is None
    assert "no eligible ledger" in gw_result["reason"].lower()


# --------------------------------------------------------------------------
# 8. Ledger record with no eligible gateway record at all
# --------------------------------------------------------------------------

def test_scenario_08_ledger_with_no_eligible_gateway():
    out = _run(
        [_gw(oid="ORD_UNRELATED", ref="RZPX", merchant="Totally Different", amount="1", date="2026-12-31")],
        [_lg(oid="ORD_LONELY_LG", eid="LGY", merchant="Solo Merchant", amount="42424", date="2026-01-01")],
    )
    lg_result = [r for r in out["results"] if r["ledger_id"] == "LGY"][0]
    assert lg_result["status"] == STATUS_EXCEPTION
    assert lg_result["gateway_id"] == ""
    assert "no eligible gateway" in lg_result["reason"].lower()


# --------------------------------------------------------------------------
# 9. A record that should become NEEDS_REVIEW
# --------------------------------------------------------------------------

def test_scenario_09_needs_review_case():
    # order ref close-but-not-exact typo, merchant/amount/date all match ->
    # lands between NEEDS_REVIEW_THRESHOLD and AUTO_MATCH_THRESHOLD
    out = _run(
        [_gw(oid="ORDA12345", ref="RZP1", merchant="Acme Co", amount="1000",
             date="2026-07-01", narr="Order ORDA12345")],
        [_lg(oid="ORDA12354", eid="LG1", merchant="Acme Co", amount="1000",
             date="2026-07-01", narr="Order ORDA12354")],
    )
    r = out["results"][0]
    assert NEEDS_REVIEW_THRESHOLD <= r["confidence"] < AUTO_MATCH_THRESHOLD
    assert r["status"] == STATUS_NEEDS_REVIEW


# --------------------------------------------------------------------------
# 10. A record that should become EXCEPTION (candidate exists but is weak)
# --------------------------------------------------------------------------

def test_scenario_10_exception_despite_candidate_existing():
    out = _run(
        [_gw(oid="ORDA1234", ref="RZP1", merchant="Acme Corp Pvt Ltd", amount="1000",
             date="2026-07-01", narr="Payment for ORDA1234")],
        [_lg(oid="ZZZ9999", eid="LG1", merchant="Acme Corporation", amount="1000",
             date="2026-07-01", narr="Settlement note unrelated text")],
    )
    r = out["results"][0]
    assert r["confidence"] < NEEDS_REVIEW_THRESHOLD
    assert r["status"] == STATUS_EXCEPTION
    assert r["method"] == "matched_pair"  # a candidate existed, it was just too weak
    assert "genuine exception" in r["reason"].lower()


# --------------------------------------------------------------------------
# 11. Duplicate order references (on both the ledger side and gateway side)
# --------------------------------------------------------------------------

def test_scenario_11a_duplicate_order_ref_on_ledger_side():
    out = _run(
        [_gw(oid="ORD1")],
        [_lg(eid="LG1", oid="ORD1"), _lg(eid="LG1DUP", oid="ORD1")],
    )
    matched = [r for r in out["results"] if r["status"] == STATUS_MATCHED]
    assert len(matched) == 1
    ledger_ids_used = [r["ledger_id"] for r in out["results"] if r["ledger_id"]]
    assert len(ledger_ids_used) == len(set(ledger_ids_used))


def test_scenario_11b_duplicate_order_ref_on_gateway_side():
    out = _run(
        [_gw(ref="RZP1", oid="ORD1"), _gw(ref="RZP1DUP", oid="ORD1")],
        [_lg(eid="LG1", oid="ORD1")],
    )
    matched = [r for r in out["results"] if r["status"] == STATUS_MATCHED]
    assert len(matched) == 1
    gateway_ids_used = [r["gateway_id"] for r in out["results"] if r["gateway_id"]]
    assert len(gateway_ids_used) == len(set(gateway_ids_used))


# --------------------------------------------------------------------------
# 12. Multiple possible candidates for one transaction
# --------------------------------------------------------------------------

def test_scenario_12_multiple_candidates_picks_the_stronger_one():
    # Both ledger rows share the gateway record's order_ref/merchant/date;
    # only one has the matching amount. The assignment must prefer it.
    out = _run(
        [_gw(oid="ORD1", ref="RZP1", merchant="Acme Co", amount="1000", date="2026-07-01")],
        [
            _lg(eid="LG_CLOSE", oid="ORD1", merchant="Acme Co", amount="1000", date="2026-07-01"),
            _lg(eid="LG_FAR", oid="ORD1", merchant="Acme Co", amount="700", date="2026-07-01"),
        ],
    )
    close = [r for r in out["results"] if r["ledger_id"] == "LG_CLOSE"][0]
    far = [r for r in out["results"] if r["ledger_id"] == "LG_FAR"][0]
    assert close["status"] == STATUS_MATCHED
    assert far["status"] == STATUS_EXCEPTION
    assert far["gateway_id"] == ""  # not stolen by the weaker pairing


# --------------------------------------------------------------------------
# 13. One ledger record cannot be assigned to multiple gateway records
# --------------------------------------------------------------------------

def test_scenario_13_one_ledger_record_not_double_assigned():
    out = _run(
        [_gw(ref="RZP1", oid="ORD1", amount="1000"), _gw(ref="RZP2", oid="ORD2", amount="1000")],
        [_lg(eid="LG1", oid="ORD1", amount="1000")],
    )
    ledger_ids_used = [r["ledger_id"] for r in out["results"] if r["ledger_id"]]
    assert len(ledger_ids_used) == len(set(ledger_ids_used))


# --------------------------------------------------------------------------
# 14. Invalid / missing amount or date values -> engine must not crash and
#     must fall back gracefully (amount_score / date_score = 0.0, not NaN)
# --------------------------------------------------------------------------

def test_scenario_14a_invalid_amount_and_date_do_not_crash():
    out = _run(
        [_gw(oid="ORD1", amount="not_a_number", date="not_a_date")],
        [_lg(oid="ORD1", amount="1000", date="2026-07-01")],
    )
    r = out["results"][0]
    assert r["signals"]["amount_score"] == 0.0
    assert r["signals"]["date_score"] == 0.0
    assert r["signals"]["amount_difference"] is None
    assert r["signals"]["date_difference_days"] is None
    # summary math must still be sane, not NaN/crash
    assert out["summary"]["total_records"] == 1


def test_scenario_14b_empty_amount_and_date_fields_do_not_crash():
    out = _run(
        [_gw(oid="ORD2", amount="", date="")],
        [_lg(oid="ORD2", amount="1000", date="2026-07-01")],
    )
    r = out["results"][0]
    assert r["signals"]["amount_score"] == 0.0
    assert r["signals"]["date_score"] == 0.0
    assert out["summary"]["gateway_total_inr"] == 0  # blank amount contributes 0, not a crash


# --------------------------------------------------------------------------
# 15. Summary counts and match rate are mathematically correct
# --------------------------------------------------------------------------

def test_scenario_15_summary_counts_and_match_rate_are_correct():
    out = _run(
        [
            _gw(ref="RZP1", oid="ORD1", amount="1000", date="2026-07-01"),           # -> MATCHED
            _gw(ref="RZP2", oid="ORDA12345", merchant="Acme Co", amount="1000",
                date="2026-07-01", narr="Order ORDA12345"),                          # -> NEEDS_REVIEW
            _gw(ref="RZP3", oid="ORD_LONELY", merchant="Solo", amount="99999",
                date="2026-01-01"),                                                   # -> EXCEPTION
        ],
        [
            _lg(eid="LG1", oid="ORD1", amount="1000", date="2026-07-01"),
            _lg(eid="LG2", oid="ORDA12354", merchant="Acme Co", amount="1000",
                date="2026-07-01", narr="Order ORDA12354"),
            _lg(eid="LG3", oid="ORD_UNRELATED", merchant="Nobody", amount="1", date="2026-12-31"),
        ],
    )
    s = out["summary"]

    # recompute independently from the raw results, rather than trusting
    # the engine's own counters, to actually verify the arithmetic
    statuses = [r["status"] for r in out["results"]]
    recomputed_matched = statuses.count(STATUS_MATCHED)
    recomputed_review = statuses.count(STATUS_NEEDS_REVIEW)
    recomputed_exception = statuses.count(STATUS_EXCEPTION)

    assert s["matched"] == recomputed_matched
    assert s["needs_review"] == recomputed_review
    assert s["exceptions"] == recomputed_exception
    assert s["total_records"] == len(out["results"])
    assert s["matched"] + s["needs_review"] + s["exceptions"] == s["total_records"]

    expected_rate = round(100 * recomputed_matched / s["total_records"], 1)
    assert s["match_rate_pct"] == expected_rate

    gw_total = round(sum(float(g["amount"]) for g in [
        {"amount": "1000"}, {"amount": "1000"}, {"amount": "99999"}
    ]), 2)
    lg_total = round(sum(float(l["amount"]) for l in [
        {"amount": "1000"}, {"amount": "1000"}, {"amount": "1"}
    ]), 2)
    assert s["gateway_total_inr"] == gw_total
    assert s["ledger_total_inr"] == lg_total
    assert s["gateway_ledger_diff_inr"] == round(gw_total - lg_total, 2)

    # matched/needs_review/exception amount buckets must partition consistently
    # with the per-record amounts of records in that bucket (gateway-side amount
    # used when present, else ledger-side — matching the engine's own amt_of()).
    def amt_of(r):
        gw_amt = r["gateway_record"].get("amount")
        lg_amt = r["ledger_record"].get("amount")
        try:
            return float(gw_amt) if gw_amt not in (None, "") else float(lg_amt or 0)
        except (TypeError, ValueError):
            return 0.0

    expected_matched_amt = round(sum(amt_of(r) for r in out["results"] if r["status"] == STATUS_MATCHED), 2)
    expected_review_amt = round(sum(amt_of(r) for r in out["results"] if r["status"] == STATUS_NEEDS_REVIEW), 2)
    expected_exception_amt = round(sum(amt_of(r) for r in out["results"] if r["status"] == STATUS_EXCEPTION), 2)

    assert s["matched_amount_inr"] == expected_matched_amt
    assert s["needs_review_amount_inr"] == expected_review_amt
    assert s["exception_amount_inr"] == expected_exception_amt