"""
Tests for the AI Finance Controller's action layer in main.py's /api/ask
route: detecting an explicit action request ("Mark ORDxxx as an
exception") and applying it via the EXISTING store.record_decision()
mechanism - the same one the manual review UI uses. No new storage/action
system; no LLM touches store state directly.

Run from backend/: python -m pytest tests/test_ai_finance_controller.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main
from config import STATUS_MATCHED, STATUS_EXCEPTION, STATUS_NEEDS_REVIEW


class _Req:
    def __init__(self, question):
        self.question = question


def _make_batch(order_ref="ORD9001", status=STATUS_MATCHED):
    out = {
        "summary": {"gateway_records": 1, "ledger_records": 1, "total_records": 1,
                    "matched": 1 if status == STATUS_MATCHED else 0,
                    "needs_review": 1 if status == STATUS_NEEDS_REVIEW else 0,
                    "exceptions": 1 if status == STATUS_EXCEPTION else 0,
                    "match_rate_pct": 100.0 if status == STATUS_MATCHED else 0.0,
                    "gateway_total_inr": 1000, "ledger_total_inr": 1000,
                    "gateway_ledger_diff_inr": 0, "matched_amount_inr": 0,
                    "needs_review_amount_inr": 0, "exception_amount_inr": 0},
        "results": [{
            "gateway_id": "GW1", "ledger_id": "LG1", "order_ref": order_ref,
            "status": status, "confidence": 0.9, "method": "matched_pair",
            "reason": "test fixture", "signals": {}, "gateway_record": {"amount": "1000"},
            "ledger_record": {"amount": "1000"}, "amount": 1000.0,
        }],
    }
    return main.store.create_batch(out, "TestGateway.csv", "TestLedger.csv")


def test_mark_as_exception_action_updates_status_and_audit_trail():
    batch = _make_batch("ORD9001", STATUS_MATCHED)
    resp = main.ask_question(_Req("Mark ORD9001 as an exception"), batch_id=batch["id"])

    assert "marked as EXCEPTION" in resp["answer"]
    assert resp["retrieval_mode"] == "action_mark_exception"
    assert resp["used_llm"] is False

    refreshed = main.store.get_batch(batch["id"])
    result = list(refreshed["results"].values())[0]
    assert result["current_status"] == STATUS_EXCEPTION
    assert result["review"]["decision"] == "mark_exception"
    assert result["review"]["reviewer"] == "ai_agent"

    audit = main.store.get_audit_trail(batch["id"])
    assert any(e["event"] == "human_review" and e["new_status"] == STATUS_EXCEPTION for e in audit)


def test_confirm_match_action_updates_status():
    batch = _make_batch("ORD9002", STATUS_NEEDS_REVIEW)
    resp = main.ask_question(_Req("Please confirm the match for ORD9002"), batch_id=batch["id"])

    assert resp["retrieval_mode"] == "action_confirm_match"
    refreshed = main.store.get_batch(batch["id"])
    result = list(refreshed["results"].values())[0]
    assert result["current_status"] == STATUS_MATCHED


def test_keep_for_review_action_updates_status():
    batch = _make_batch("ORD9003", STATUS_EXCEPTION)
    resp = main.ask_question(_Req("Keep ORD9003 for review"), batch_id=batch["id"])

    assert resp["retrieval_mode"] == "action_keep_for_review"
    refreshed = main.store.get_batch(batch["id"])
    result = list(refreshed["results"].values())[0]
    assert result["current_status"] == STATUS_NEEDS_REVIEW


def test_action_on_nonexistent_order_ref_does_not_crash_or_fabricate_success():
    batch = _make_batch("ORD9004", STATUS_MATCHED)
    resp = main.ask_question(_Req("Mark ORD9999999 as an exception"), batch_id=batch["id"])

    assert resp["retrieval_mode"] == "action_not_found"
    assert "couldn't find" in resp["answer"].lower()
    # nothing in the batch changed
    refreshed = main.store.get_batch(batch["id"])
    result = list(refreshed["results"].values())[0]
    assert result["current_status"] == STATUS_MATCHED


def test_plain_question_without_action_intent_falls_through_to_qa_agent():
    batch = _make_batch("ORD9005", STATUS_MATCHED)
    resp = main.ask_question(_Req("What's the match rate?"), batch_id=batch["id"])

    assert resp["retrieval_mode"] == "match_rate"


def test_analyze_batch_question_reaches_priority_analysis_mode():
    batch = _make_batch("ORD9006", STATUS_EXCEPTION)
    resp = main.ask_question(_Req("Analyze this batch and tell me what needs my attention."),
                              batch_id=batch["id"])

    assert resp["retrieval_mode"] == "batch_priority_analysis"
    assert "Batch analyzed." in resp["answer"]
    assert "ORD9006" in resp["answer"]
