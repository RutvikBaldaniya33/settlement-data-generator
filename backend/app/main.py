"""
SettleSense API v2 - Razorpay AI Buildathon, Track 04 (AI Finance Controller)

Workflow: upload CSVs -> validate -> create batch -> reconcile -> review ->
decide -> audit trail -> ask questions. Read-only / analysis-only: this
service never initiates a payout, refund, or ledger write.
"""
import os
import sys
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))
from matching import reconcile
from qa_agent import ask as qa_ask
from validation import validate_csv
from razorpay_client import (
    test_connection as razorpay_test_connection,
    RazorpayConfigError, RazorpayAuthError, RazorpayAPIError,
)
import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DEFAULT_GATEWAY_CSV = os.path.join(DATA_DIR, "gateway_settlements.csv")
DEFAULT_LEDGER_CSV = os.path.join(DATA_DIR, "internal_ledger.csv")

app = FastAPI(title="SettleSense", description="AI Finance Controller - settlement reconciliation, review, and Q&A")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo only - restrict before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    # Never leak a raw Python traceback to the client - log it server-side,
    # return a generic, safe message instead.
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"error": "An unexpected server error occurred. Please try again."})


class AskRequest(BaseModel):
    question: str


class ReviewDecisionRequest(BaseModel):
    result_key: str
    decision: str  # confirm_match | mark_exception | keep_for_review
    reviewer: str = "operator"
    note: str = ""


def _ensure_default_batch():
    """On first request, if no batch exists yet, reconcile the shipped
    synthetic dataset so the dashboard has something to show immediately -
    matches the existing demo workflow, doesn't break it."""
    if store.latest_batch() is None:
        if os.path.exists(DEFAULT_GATEWAY_CSV) and os.path.exists(DEFAULT_LEDGER_CSV):
            out = reconcile(DEFAULT_GATEWAY_CSV, DEFAULT_LEDGER_CSV)
            store.create_batch(out, "gateway_settlements.csv (default)", "internal_ledger.csv (default)")


def _batch_view(batch: dict) -> dict:
    """Batch with current (possibly human-reviewed) statuses reflected in
    the summary counts, not just the original system recommendation."""
    results = list(batch["results"].values())
    matched = sum(1 for r in results if r["current_status"] == "MATCHED")
    needs_review = sum(1 for r in results if r["current_status"] == "NEEDS_REVIEW")
    exceptions = sum(1 for r in results if r["current_status"] == "EXCEPTION")
    total = len(results)

    def amt(r):
        gw = r.get("gateway_record") or {}
        ld = r.get("ledger_record") or {}
        try:
            return float(gw.get("amount") or ld.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0

    live_summary = dict(batch["summary"])
    live_summary.update({
        "matched": matched, "needs_review": needs_review, "exceptions": exceptions,
        "match_rate_pct": round(100 * matched / total, 1) if total else 0.0,
        "matched_amount_inr": round(sum(amt(r) for r in results if r["current_status"] == "MATCHED"), 2),
        "needs_review_amount_inr": round(sum(amt(r) for r in results if r["current_status"] == "NEEDS_REVIEW"), 2),
        "exception_amount_inr": round(sum(amt(r) for r in results if r["current_status"] == "EXCEPTION"), 2),
    })

    return {
        "id": batch["id"], "created_at": batch["created_at"],
        "gateway_source": batch["gateway_source"], "ledger_source": batch["ledger_source"],
        "gateway_count": batch["gateway_count"], "ledger_count": batch["ledger_count"],
        "summary": live_summary,
        "results": results,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---- Razorpay TEST MODE connection check -----------------------------------
# Step 3 scope: credential/connectivity check ONLY. Does not fetch or
# reconcile any Razorpay data, and does not touch matching.py/normalize.py.
# All Razorpay-specific logic lives in razorpay_client.py.

@app.get("/api/razorpay/test-connection")
def razorpay_connection_check():
    """Confirms RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (read from environment
    variables only) are present, look like TEST mode keys, and are accepted
    by Razorpay. Never returns the secret to the caller."""
    try:
        return razorpay_test_connection()
    except RazorpayConfigError as e:
        raise HTTPException(400, str(e))
    except RazorpayAuthError as e:
        raise HTTPException(401, str(e))
    except RazorpayAPIError as e:
        raise HTTPException(502, str(e))


# ---- Upload & validation ---------------------------------------------------

@app.post("/api/validate")
async def validate_upload(file: UploadFile = File(...), kind: str = Query(..., pattern="^(gateway|ledger)$")):
    content = await file.read()
    result = validate_csv(content, kind)
    return result


@app.post("/api/batches")
async def create_batch_from_upload(gateway_file: UploadFile = File(...), ledger_file: UploadFile = File(...)):
    gw_bytes = await gateway_file.read()
    lg_bytes = await ledger_file.read()

    gw_validation = validate_csv(gw_bytes, "gateway")
    lg_validation = validate_csv(lg_bytes, "ledger")
    if not gw_validation["valid"] or not lg_validation["valid"]:
        raise HTTPException(400, detail={
            "error": "Validation failed",
            "gateway_errors": gw_validation["errors"],
            "ledger_errors": lg_validation["errors"],
        })

    # persist uploaded files to a temp batch-scoped path so reconcile() (which
    # reads from disk) can use them
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="settlesense_upload_")
    gw_path = os.path.join(tmp_dir, "gateway.csv")
    lg_path = os.path.join(tmp_dir, "ledger.csv")
    with open(gw_path, "wb") as f:
        f.write(gw_bytes)
    with open(lg_path, "wb") as f:
        f.write(lg_bytes)

    try:
        out = reconcile(gw_path, lg_path)
    except Exception as e:
        raise HTTPException(500, detail=f"Reconciliation failed: {e}")

    batch = store.create_batch(out, gateway_file.filename, ledger_file.filename)
    return _batch_view(batch)


# ---- Batches ----------------------------------------------------------------

@app.get("/api/batches")
def list_batches():
    _ensure_default_batch()
    return {"batches": [
        {"id": b["id"], "created_at": b["created_at"], "gateway_source": b["gateway_source"],
         "ledger_source": b["ledger_source"], "summary": b["summary"]}
        for b in store.list_batches()
    ]}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    _ensure_default_batch()
    batch = store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    return _batch_view(batch)


@app.post("/api/batches/{batch_id}/rerun")
def rerun_batch(batch_id: str):
    """Safely re-run reconciliation for a batch's original source files.
    Creates a NEW batch rather than mutating the old one, so nothing is
    silently overwritten and the audit trail stays intact."""
    _ensure_default_batch()
    old = store.get_batch(batch_id)
    if old is None:
        raise HTTPException(404, "Batch not found")
    if old["gateway_source"].endswith("(default)"):
        out = reconcile(DEFAULT_GATEWAY_CSV, DEFAULT_LEDGER_CSV)
        new_batch = store.create_batch(out, old["gateway_source"], old["ledger_source"])
        return _batch_view(new_batch)
    raise HTTPException(400, "Re-run is only supported for the default demo dataset in this MVP; "
                              "re-upload the same files to re-run an uploaded batch.")


# ---- Reconciliation results (default batch convenience endpoints) ---------

@app.get("/api/reconcile")
def get_reconciliation(refresh: bool = False, batch_id: str = None):
    _ensure_default_batch()
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise HTTPException(404, "No batch available")
    view = _batch_view(batch)
    return {"summary": view["summary"], "results": view["results"], "batch_id": batch["id"]}


@app.get("/api/results")
def get_results(
    batch_id: str = None,
    status: str = Query(None, pattern="^(MATCHED|NEEDS_REVIEW|EXCEPTION)$"),
    search: str = None,
    merchant: str = None,
    min_confidence: float = None,
    max_confidence: float = None,
    amount_mismatch_only: bool = False,
    date_mismatch_only: bool = False,
):
    """Search & filter across a batch's results."""
    _ensure_default_batch()
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise HTTPException(404, "No batch available")

    results = list(batch["results"].values())

    if status:
        results = [r for r in results if r["current_status"] == status]
    if search:
        s = search.lower()
        results = [r for r in results if s in (r.get("order_ref") or "").lower()
                   or s in (r.get("gateway_id") or "").lower()
                   or s in (r.get("ledger_id") or "").lower()]
    if merchant:
        m = merchant.lower()
        results = [r for r in results if m in ((r.get("gateway_record") or {}).get("merchant", "") +
                                                 (r.get("ledger_record") or {}).get("merchant", "")).lower()]
    if min_confidence is not None:
        results = [r for r in results if r["confidence"] >= min_confidence]
    if max_confidence is not None:
        results = [r for r in results if r["confidence"] <= max_confidence]
    if amount_mismatch_only:
        results = [r for r in results if (r.get("signals") or {}).get("amount_difference") not in (None, 0, 0.0)]
    if date_mismatch_only:
        results = [r for r in results if (r.get("signals") or {}).get("date_difference_days") not in (None, 0)]

    return {"count": len(results), "results": results, "batch_id": batch["id"]}


@app.get("/api/exceptions")
def get_exceptions(batch_id: str = None):
    return get_results(batch_id=batch_id, status="EXCEPTION")


@app.get("/api/needs-review")
def get_needs_review(batch_id: str = None):
    return get_results(batch_id=batch_id, status="NEEDS_REVIEW")


# ---- Human review -----------------------------------------------------------

@app.post("/api/batches/{batch_id}/review")
def submit_review_decision(batch_id: str, req: ReviewDecisionRequest):
    if req.decision not in ("confirm_match", "mark_exception", "keep_for_review"):
        raise HTTPException(400, "decision must be one of: confirm_match, mark_exception, keep_for_review")
    result = store.record_decision(batch_id, req.result_key, req.decision, req.reviewer, req.note)
    if result is None:
        raise HTTPException(404, "Batch or result not found")
    return result


# ---- Audit trail --------------------------------------------------------

@app.get("/api/audit-trail")
def get_audit_trail(batch_id: str = None):
    _ensure_default_batch()
    return {"audit_trail": store.get_audit_trail(batch_id)}


# ---- Q&A --------------------------------------------------------------------

@app.post("/api/ask")
def ask_question(req: AskRequest, batch_id: str = None):
    _ensure_default_batch()
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise HTTPException(404, "No batch available")
    results = list(batch["results"].values())
    # Q&A reasons over current_status (post human-review), not the stale
    # original system_status, so answers reflect reality.
    for r in results:
        r["status"] = r["current_status"]
    return qa_ask(req.question, results)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)