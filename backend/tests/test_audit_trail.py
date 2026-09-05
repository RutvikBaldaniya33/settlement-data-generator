"""
Regression tests for the `batch_created` audit-trail event's reason text.

Bug being guarded against: a persisted batch's audit "batch_created" entry
must describe THAT batch's own actual gateway/ledger sources and counts —
never another batch's, and never something hardcoded. store.create_batch()
builds this text purely from its own parameters (gateway_source,
ledger_source, and the reconciliation output's own record counts) — this
file proves that holds for the synthetic CSV flow, the Razorpay flow, and
an arbitrary third source name (to prove genericness, not a special case
for either of the first two).

No real network calls are made — Razorpay's fetch_payments() is always
mocked.

Run from backend/: python -m pytest tests/test_audit_trail.py -v
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

TEST_KEY_ID = "rzp_test_abcdefgh1234"
TEST_KEY_SECRET = "supersecretvalue"


def _razorpay_payment(id="pay_A1", order_id="ORD1001", amount_paise="150050",
                       description="Order ORD1001", email="buyer@example.com",
                       created_at=1751328000):
    return {
        "id": id, "entity": "payment", "amount": amount_paise, "currency": "INR",
        "status": "captured", "order_id": order_id, "description": description,
        "email": email, "contact": "+919999999999", "created_at": created_at,
    }


def _created_event(audit_events):
    created = [e for e in audit_events if e["event"] == "batch_created"]
    assert created, "expected a batch_created audit event"
    return created[-1]  # most recent, in case of re-runs


# --------------------------------------------------------------------------
# 1. Synthetic CSV (default demo) batch audit metadata
# --------------------------------------------------------------------------

def test_synthetic_default_batch_audit_metadata_is_correct():
    import main
    # This test specifically exercises _ensure_default_batch()'s own
    # batch-creation behavior, which is a no-op once ANY batch already
    # exists in the shared in-memory store - and other test files (or
    # earlier tests in this file) may have already created one. Start
    # from a clean store so this test is deterministic regardless of
    # what ran before it; this doesn't touch any production code.
    main.store._batches.clear()
    main.store._audit_log.clear()
    main._ensure_default_batch()

    default_batches = [
        b for b in main.store.list_batches()
        if b["gateway_source"] == "gateway_settlements.csv (default)"
    ]
    assert default_batches, "expected the default demo batch to exist"
    batch = default_batches[0]

    audit = main.store.get_audit_trail(batch["id"])
    reason = _created_event(audit)["reason"]

    assert "gateway_settlements.csv (default)" in reason
    assert "internal_ledger.csv (default)" in reason
    # counts in the audit text must match THIS batch's own actual counts,
    # not any other batch's
    assert f"{batch['gateway_count']} gateway" in reason
    assert f"{batch['ledger_count']} ledger" in reason


# --------------------------------------------------------------------------
# 2. Razorpay batch audit metadata
# --------------------------------------------------------------------------

def test_razorpay_batch_audit_metadata_is_correct(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    import main

    fake_payments = [
        _razorpay_payment(id="pay_1", order_id="ORD1"),
        _razorpay_payment(id="pay_2", order_id="ORD2"),
        _razorpay_payment(id="pay_3", order_id="ORD3"),
    ]
    with patch("main.razorpay_fetch_payments", return_value=fake_payments) as mock_fetch:
        result = main.razorpay_reconcile(count=3)
        mock_fetch.assert_called_once_with(count=3)

    assert result["gateway_source"] == "Razorpay TEST"
    assert result["ledger_source"] == "internal_ledger.csv (default)"
    assert result["gateway_count"] == 3  # actual fetched count, not the default CSV's

    audit = main.store.get_audit_trail(batch_id=result["id"])
    reason = _created_event(audit)["reason"]

    assert "Razorpay TEST" in reason
    assert "internal_ledger.csv (default)" in reason
    # must reflect THIS Razorpay batch's own actual counts (3 gateway
    # records), never the unrelated default CSV batch's counts
    assert f"{result['gateway_count']} gateway" in reason
    assert f"{result['ledger_count']} ledger" in reason
    assert "gateway_settlements.csv" not in reason


def test_razorpay_batch_gateway_count_reflects_actual_fetch_not_default_csv(monkeypatch):
    """A different fetch size must produce a different, accurate count in
    both the batch and its audit text - proving the numbers are computed
    from the real reconciliation output, not copied from another batch."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    import main

    fake_payments = [_razorpay_payment(id=f"pay_{i}", order_id=f"ORD{i}") for i in range(5)]
    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        result = main.razorpay_reconcile(count=5)

    assert result["gateway_count"] == 5
    reason = _created_event(main.store.get_audit_trail(batch_id=result["id"]))["reason"]
    assert "5 gateway" in reason


# --------------------------------------------------------------------------
# 3. The audit-reason logic itself is generic - not special-cased for
#    "razorpay" or the default CSV filenames.
# --------------------------------------------------------------------------

def test_audit_reason_generation_is_source_agnostic_not_hardcoded():
    """store.create_batch()'s audit text must be driven purely by whatever
    gateway_source/ledger_source/counts it's given - proven here with a
    source name that isn't "razorpay" or a default CSV filename at all."""
    import store

    fake_output = {
        "summary": {"gateway_records": 7, "ledger_records": 9, "total_records": 16,
                    "matched": 0, "needs_review": 0, "exceptions": 16,
                    "match_rate_pct": 0.0, "gateway_total_inr": 0, "ledger_total_inr": 0,
                    "gateway_ledger_diff_inr": 0, "matched_amount_inr": 0,
                    "needs_review_amount_inr": 0, "exception_amount_inr": 0},
        "results": [],
    }
    batch = store.create_batch(fake_output, "SomeOtherGateway.csv", "SomeOtherLedger.csv")
    reason = _created_event(store.get_audit_trail(batch["id"]))["reason"]

    assert "SomeOtherGateway.csv" in reason
    assert "SomeOtherLedger.csv" in reason
    assert "7 gateway" in reason
    assert "9 ledger" in reason
    assert "razorpay" not in reason.lower()
    assert "gateway_settlements" not in reason


# --------------------------------------------------------------------------
# 4. No secret ever appears in the audit trail
# --------------------------------------------------------------------------

def test_no_secret_appears_in_razorpay_batch_audit_trail(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    import main

    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]
    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        result = main.razorpay_reconcile(count=1)

    audit = main.get_audit_trail(batch_id=result["id"])
    dumped = json.dumps(audit, default=str)
    assert TEST_KEY_SECRET not in dumped
    assert TEST_KEY_ID not in dumped
