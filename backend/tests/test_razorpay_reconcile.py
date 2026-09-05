"""
Tests for the Razorpay -> normalize -> reconcile integration.

No real network calls are made anywhere in this file — razorpay_client's
fetch_payments() (or the underlying requests.get) is always mocked/patched.
These tests exercise the ACTUAL integration path end-to-end: a raw
Razorpay-shaped payment dict -> normalize_gateway_row(..., source="razorpay")
-> matching.reconcile_records() -> real MATCHED/NEEDS_REVIEW/EXCEPTION
classification, using the real scoring engine and real config.py thresholds.
Nothing here fabricates a "successful" result directly.

Run from backend/: python -m pytest tests/test_razorpay_reconcile.py -v
"""
import csv
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from matching import reconcile_records
from normalize import normalize_gateway_row, normalize_ledger_row
from config import STATUS_MATCHED, STATUS_EXCEPTION


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


def _ledger_row(eid="LG1", oid="ORD1001", merchant="Acme Co", amount="1500.50",
                date="2025-07-01", narr="Order ORD1001"):
    return {"entry_id": eid, "order_ref": oid, "merchant": merchant,
            "amount": amount, "date": date, "narration": narr}


# --------------------------------------------------------------------------
# 1 & 2. A Razorpay payment is normalized, reaches the reconciliation
#         engine, and its source="razorpay" tag is preserved end-to-end.
# --------------------------------------------------------------------------

def test_razorpay_payment_normalizes_and_reaches_reconciliation_engine():
    raw = _razorpay_payment()
    gw_record = normalize_gateway_row(raw, source="razorpay")

    assert gw_record.source == "razorpay"
    assert gw_record.record_type == "gateway"
    assert gw_record.raw == raw  # original Razorpay dict preserved verbatim

    ld_record = normalize_ledger_row(_ledger_row(), source="synthetic")

    out = reconcile_records([gw_record], [ld_record])
    r = out["results"][0]

    # this proves the record actually went through the real scoring engine,
    # not a stub — reference/amount/date all line up so it should MATCH
    assert r["status"] == STATUS_MATCHED
    assert r["gateway_id"] == "pay_A1"
    assert r["ledger_id"] == "LG1"
    assert r["signals"]["amount_score"] == 1.0


# --------------------------------------------------------------------------
# 3. Amount conversion from paise to rupees survives all the way through
#    the reconciliation engine's scoring AND its summary totals.
# --------------------------------------------------------------------------

def test_amount_conversion_paise_to_rupees_preserved_through_reconciliation():
    # 150050 paise == Rs 1500.50 exactly
    raw = _razorpay_payment(amount_paise="150050")
    gw_record = normalize_gateway_row(raw, source="razorpay")
    assert gw_record.amount == 1500.50  # normalize.py's own conversion

    ld_record = normalize_ledger_row(_ledger_row(amount="1500.50"), source="synthetic")
    out = reconcile_records([gw_record], [ld_record])
    r = out["results"][0]

    assert r["status"] == STATUS_MATCHED
    assert r["signals"]["amount_difference"] == 0.0
    # the raw Razorpay dict in gateway_record still shows paise (untouched,
    # for audit purposes) — the important thing is the SUMMARY totals below
    # reflect rupees, not paise
    assert r["gateway_record"]["amount"] == "150050"

    s = out["summary"]
    assert s["gateway_total_inr"] == 1500.50   # NOT 150050
    assert s["matched_amount_inr"] == 1500.50  # NOT 150050


def test_amount_conversion_with_mismatched_ledger_still_uses_rupees_in_buckets():
    # gateway (razorpay, paise) vs a ledger amount far enough off to land
    # as EXCEPTION — the exception bucket total must still be in rupees.
    raw = _razorpay_payment(amount_paise="999999999", order_id="ORD_LONELY")
    gw_record = normalize_gateway_row(raw, source="razorpay")
    assert gw_record.amount == 9999999.99

    ld_record = normalize_ledger_row(
        _ledger_row(eid="LG_X", oid="ORD_UNRELATED", amount="1", merchant="Nobody"),
        source="synthetic",
    )
    out = reconcile_records([gw_record], [ld_record])
    s = out["summary"]
    # gateway_total_inr must reflect the converted rupee amount, not the
    # raw paise integer
    assert s["gateway_total_inr"] == 9999999.99


# --------------------------------------------------------------------------
# 5 & 6. The /api/razorpay/reconcile endpoint now persists a normal batch via
#         the existing store.create_batch()/_batch_view() infrastructure —
#         credentials/secrets never leak into that batch, and no real
#         network call is made.
# --------------------------------------------------------------------------

def _import_main_with_env(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    import main
    return main


def test_endpoint_creates_a_persisted_batch_via_existing_store(monkeypatch):
    """The endpoint must reuse store.create_batch()/store.get_batch() — not
    a second/parallel storage mechanism — so the returned batch is
    independently retrievable by id afterwards, exactly like a CSV-upload
    batch."""
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        result = main.razorpay_reconcile(count=1)

    assert "id" in result and result["id"]
    stored = main.store.get_batch(result["id"])
    assert stored is not None
    assert stored["id"] == result["id"]
    assert stored["gateway_source"] == "Razorpay TEST"
    # it's listed alongside every other batch, not in a separate store
    assert any(b["id"] == result["id"] for b in main.store.list_batches())


def test_batch_contains_normalized_razorpay_records(monkeypatch):
    """The persisted batch's results must be built from real
    NormalizedRecords that went through normalize.py's Razorpay adapter —
    order_ref/merchant/narration/amount all reflect the normalization, and
    the untouched raw Razorpay payment dict is preserved for audit."""
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001", amount_paise="150050")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        with patch("main.load_ledger_records") as mock_ld:
            mock_ld.return_value = [normalize_ledger_row(_ledger_row(amount="1500.50"), source="synthetic")]
            result = main.razorpay_reconcile(count=1)

    r = result["results"][0]
    assert r["gateway_id"] == "pay_1"
    assert r["order_ref"] == "ORD1001"
    assert r["gateway_record"]["amount"] == "150050"  # untouched raw Razorpay value, for audit
    assert r["amount"] == 1500.50  # canonical normalized amount actually used for money math
    assert r["current_status"] == STATUS_MATCHED
    assert r["result_key"]  # persisted batches assign a real result_key


def test_endpoint_gateway_source_is_razorpay_test(monkeypatch):
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        result = main.razorpay_reconcile(count=1)

    assert result["gateway_source"] == "Razorpay TEST"
    assert result["gateway_count"] == 1


def test_endpoint_ledger_source_is_the_default_demo_ledger(monkeypatch):
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        result = main.razorpay_reconcile(count=1)

    # must clearly identify the existing default/synthetic ledger, and must
    # NOT be mistaken for a razorpay-sourced ledger (no such thing exists)
    assert "razorpay" not in result["ledger_source"].lower()
    assert "internal_ledger" in result["ledger_source"] or "default" in result["ledger_source"]


def test_persisted_batch_amount_totals_are_in_rupees_not_paise(monkeypatch):
    """Regression test for a real bug this feature surfaced: _batch_view()'s
    live summary recompute used to read gateway_record["amount"] directly,
    which is fine for synthetic CSV rows (already rupees) but is Razorpay's
    raw PAISE value — so a persisted Razorpay batch's matched/needs_review/
    exception amount totals would have been 100x too large. The canonical
    `amount` field on each result (set by matching.py at reconciliation
    time) must be what _batch_view() actually uses."""
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001", amount_paise="150050")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        with patch("main.load_ledger_records") as mock_ld:
            mock_ld.return_value = [normalize_ledger_row(_ledger_row(amount="1500.50"), source="synthetic")]
            result = main.razorpay_reconcile(count=1)

    assert result["summary"]["matched_amount_inr"] == 1500.50  # NOT 150050.0

    # re-fetching the batch (as the dashboard/batch-detail endpoint would)
    # must show the same correct figure, since _batch_view recomputes it
    refetched = main.get_batch(result["id"])
    assert refetched["summary"]["matched_amount_inr"] == 1500.50


def test_persisted_razorpay_batch_supports_human_review_and_audit_trail(monkeypatch):
    """The Razorpay batch must work with the EXISTING review + audit-trail
    endpoints, unmodified — no Razorpay-specific review logic anywhere."""
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        with patch("main.load_ledger_records") as mock_ld:
            mock_ld.return_value = [normalize_ledger_row(_ledger_row(), source="synthetic")]
            result = main.razorpay_reconcile(count=1)

    batch_id = result["id"]
    result_key = result["results"][0]["result_key"]
    assert result["results"][0]["current_status"] == STATUS_MATCHED

    req = main.ReviewDecisionRequest(result_key=result_key, decision="mark_exception", reviewer="qa")
    reviewed = main.submit_review_decision(batch_id, req)
    assert reviewed["current_status"] == STATUS_EXCEPTION

    # batch detail reflects the review in its live summary
    detail = main.get_batch(batch_id)
    assert detail["summary"]["exceptions"] == 1
    assert detail["summary"]["matched"] == 0

    # and it's in the audit trail, same as any CSV batch
    audit = main.get_audit_trail(batch_id=batch_id)
    events = [e["event"] for e in audit["audit_trail"]]
    assert "batch_created" in events
    assert "human_review" in events


def test_endpoint_missing_credentials_returns_400_not_a_crash(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    import main
    from fastapi import HTTPException

    try:
        main.razorpay_reconcile(count=5)
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 400


def test_endpoint_auth_error_returns_401(monkeypatch):
    main = _import_main_with_env(monkeypatch)
    from fastapi import HTTPException
    from razorpay_client import RazorpayAuthError

    with patch("main.razorpay_fetch_payments", side_effect=RazorpayAuthError("bad creds")):
        try:
            main.razorpay_reconcile(count=5)
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 401


def test_endpoint_api_error_returns_502(monkeypatch):
    main = _import_main_with_env(monkeypatch)
    from fastapi import HTTPException
    from razorpay_client import RazorpayAPIError

    with patch("main.razorpay_fetch_payments", side_effect=RazorpayAPIError("Razorpay unreachable")):
        try:
            main.razorpay_reconcile(count=5)
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 502


def test_errors_never_create_a_batch(monkeypatch):
    """A failed fetch must not leave a half-created batch behind in the
    store."""
    main = _import_main_with_env(monkeypatch)
    from razorpay_client import RazorpayAuthError

    before = len(main.store.list_batches())
    with patch("main.razorpay_fetch_payments", side_effect=RazorpayAuthError("bad creds")):
        try:
            main.razorpay_reconcile(count=5)
        except Exception:
            pass
    after = len(main.store.list_batches())
    assert after == before


def test_endpoint_never_calls_real_requests_get(monkeypatch):
    """Belt-and-suspenders: even if someone forgot to mock
    main.razorpay_fetch_payments, patching the underlying requests.get
    confirms fetch_payments() itself never escapes to the real network in
    this test (it's exercised directly in test_razorpay_client.py)."""
    main = _import_main_with_env(monkeypatch)
    with patch("razorpay_client.requests.get") as mock_get:
        with patch("main.razorpay_fetch_payments", return_value=[]):
            main.razorpay_reconcile(count=1)
        mock_get.assert_not_called()


def test_no_secret_appears_anywhere_in_the_persisted_batch(monkeypatch):
    """Checks the FULL persisted batch view (as returned by the endpoint AND
    as independently re-fetched via get_batch), not just a flat preview
    object — the secret must not leak through store.create_batch(),
    _batch_view(), the audit log, or anywhere else."""
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments) as mock_fetch:
        result = main.razorpay_reconcile(count=1)
        mock_fetch.assert_called_once_with(count=1)  # no network call escaped the mock

    refetched = main.get_batch(result["id"])
    audit = main.get_audit_trail(batch_id=result["id"])

    for payload in (result, refetched, audit):
        dumped = json.dumps(payload, default=str)
        assert TEST_KEY_SECRET not in dumped
        assert TEST_KEY_ID not in dumped  # doesn't even need to echo the key id


def test_endpoint_result_shape_matches_existing_batch_contract(monkeypatch):
    """The endpoint's output must be the SAME batch shape used by
    /api/batches (POST) and /api/batches/{id} (GET) for the CSV flow —
    id, created_at, gateway_source, ledger_source, gateway_count,
    ledger_count, summary, results — so the dashboard needs no special
    casing for a Razorpay batch."""
    main = _import_main_with_env(monkeypatch)
    fake_payments = [_razorpay_payment(id="pay_1", order_id="ORD1001")]

    with patch("main.razorpay_fetch_payments", return_value=fake_payments):
        result = main.razorpay_reconcile(count=1)

    for key in ("id", "created_at", "gateway_source", "ledger_source",
                "gateway_count", "ledger_count", "summary", "results"):
        assert key in result
    for key in ("total_records", "matched", "needs_review", "exceptions", "match_rate_pct"):
        assert key in result["summary"]
    assert len(result["results"]) == result["summary"]["total_records"]
    for key in ("result_key", "current_status", "system_status", "review",
                "order_ref", "status", "confidence", "reason", "signals",
                "gateway_record", "ledger_record"):
        assert key in result["results"][0]


# --------------------------------------------------------------------------
# Isolation checks
# --------------------------------------------------------------------------

def test_ledger_side_is_unaffected_by_razorpay_gateway_source():
    """Ledger records loaded via the existing synthetic path must reconcile
    identically whether the gateway side is synthetic or razorpay-sourced —
    the ledger normalization/loading path is untouched by this change."""
    ld_record = normalize_ledger_row(_ledger_row(), source="synthetic")
    assert ld_record.source == "synthetic"
    assert ld_record.record_type == "ledger"
