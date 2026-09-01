"""
In-memory batch, decision, and audit-trail store.

MVP-appropriate: a single-process dict-backed store, not a database. This is
a documented limitation (see README) — state resets on server restart. For
a real deployment this would move to Postgres/SQLite with the same shape.
"""
import uuid
from datetime import datetime, timezone

_batches = {}     # batch_id -> {id, created_at, gateway_count, ledger_count, summary, results (dict keyed by result_key)}
_audit_log = []    # list of audit event dicts, append-only from this module's perspective


def _now():
    return datetime.now(timezone.utc).isoformat()


def _result_key(r: dict) -> str:
    """Stable identity for a result row within a batch, for lookups/decisions."""
    return f"{r.get('gateway_id') or 'NOGW'}::{r.get('ledger_id') or 'NOLG'}::{r.get('order_ref')}"


def create_batch(reconciliation_output: dict, gateway_source: str, ledger_source: str) -> dict:
    batch_id = f"BATCH-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    results_by_key = {}
    for r in reconciliation_output["results"]:
        r = dict(r)  # copy
        r["result_key"] = _result_key(r)
        r["system_status"] = r["status"]      # original system recommendation, preserved forever
        r["current_status"] = r["status"]     # can change via human review
        r["review"] = None                    # {decision, reviewer, timestamp, note} once reviewed
        results_by_key[r["result_key"]] = r

    batch = {
        "id": batch_id,
        "created_at": _now(),
        "gateway_source": gateway_source,
        "ledger_source": ledger_source,
        "gateway_count": reconciliation_output["summary"]["gateway_records"],
        "ledger_count": reconciliation_output["summary"]["ledger_records"],
        "summary": reconciliation_output["summary"],
        "results": results_by_key,
    }
    _batches[batch_id] = batch

    _audit_log.append({
        "timestamp": _now(), "batch_id": batch_id, "event": "batch_created",
        "gateway_id": None, "ledger_id": None, "order_ref": None,
        "previous_status": None, "new_status": None,
        "system_recommendation": None, "human_decision": None,
        "method": None, "confidence": None, "reason": f"Batch created from "
        f"{gateway_source} + {ledger_source} "
        f"({batch['gateway_count']} gateway / {batch['ledger_count']} ledger records).",
        "reviewer": "system",
    })
    return batch


def get_batch(batch_id: str):
    return _batches.get(batch_id)


def list_batches():
    return sorted(_batches.values(), key=lambda b: b["created_at"], reverse=True)


def latest_batch():
    batches = list_batches()
    return batches[0] if batches else None


def record_decision(batch_id: str, result_key: str, decision: str, reviewer: str, note: str = ""):
    """decision is one of: confirm_match, mark_exception, keep_for_review."""
    batch = _batches.get(batch_id)
    if batch is None:
        return None
    result = batch["results"].get(result_key)
    if result is None:
        return None

    previous_status = result["current_status"]
    decision_to_status = {
        "confirm_match": "MATCHED",
        "mark_exception": "EXCEPTION",
        "keep_for_review": "NEEDS_REVIEW",
    }
    new_status = decision_to_status.get(decision)
    if new_status is None:
        return None

    result["current_status"] = new_status
    result["review"] = {
        "decision": decision, "reviewer": reviewer, "timestamp": _now(), "note": note,
    }

    _audit_log.append({
        "timestamp": _now(), "batch_id": batch_id, "event": "human_review",
        "gateway_id": result.get("gateway_id"), "ledger_id": result.get("ledger_id"),
        "order_ref": result.get("order_ref"),
        "previous_status": previous_status, "new_status": new_status,
        "system_recommendation": result["system_status"], "human_decision": decision,
        "method": result.get("method"), "confidence": result.get("confidence"),
        "reason": result.get("reason"), "reviewer": reviewer,
    })
    return result


def get_audit_trail(batch_id: str = None):
    if batch_id:
        return [e for e in _audit_log if e["batch_id"] == batch_id]
    return list(_audit_log)
