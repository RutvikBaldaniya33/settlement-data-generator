"""
Tests for the Settlement Q&A agent.
Run from backend/: python -m pytest tests/ -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from qa_agent import ask
from config import STATUS_MATCHED, STATUS_NEEDS_REVIEW, STATUS_EXCEPTION


def _row(order_ref, status, confidence=0.9, amount=1000, method="matched_pair"):
    return {
        "order_ref": order_ref, "current_status": status, "status": status,
        "confidence": confidence, "method": method, "reason": "test reason",
        "signals": {"amount_difference": 0, "date_difference_days": 0},
        "gateway_record": {"merchant": "Acme Co", "amount": str(amount)},
        "ledger_record": {"merchant": "Acme Co", "amount": str(amount)},
    }


def test_direct_order_ref_lookup_no_hallucination():
    rows = [_row("ORD1", STATUS_EXCEPTION)]
    out = ask("Why is ORD1 an exception?", rows)
    assert "ORD1" in out["answer"]
    assert out["source_records"] == ["ORD1"]
    assert out["used_llm"] is False  # no API key configured in test env


def test_match_rate_question_states_exact_percentage():
    rows = [_row("ORD1", STATUS_MATCHED), _row("ORD2", STATUS_MATCHED),
            _row("ORD3", STATUS_EXCEPTION), _row("ORD4", STATUS_EXCEPTION)]
    out = ask("What is the match rate?", rows)
    assert "50.0%" in out["answer"]
    assert out["used_llm"] is False


def test_zero_needs_review_answers_honestly_not_unrelated_records():
    """The critical regression: when nothing needs review, the agent must say
    so, not silently substitute unrelated matched/exception records."""
    rows = [_row("ORD1", STATUS_MATCHED), _row("ORD2", STATUS_EXCEPTION)]
    out = ask("How many transactions need review?", rows)
    assert "0" in out["answer"]
    assert "need review" in out["answer"]
    assert out["source_records"] == []


def test_how_much_in_exception_sums_only_exceptions():
    rows = [_row("ORD1", STATUS_EXCEPTION, amount=1000),
            _row("ORD2", STATUS_EXCEPTION, amount=2000),
            _row("ORD3", STATUS_MATCHED, amount=5000)]
    out = ask("How much money is currently in exception?", rows)
    assert "3,000" in out["answer"] or "3000" in out["answer"]
    assert "2 record" in out["answer"]


def test_totals_comparison_is_deterministic():
    rows = [_row("ORD1", STATUS_MATCHED, amount=1000)]
    out = ask("Compare gateway and ledger totals", rows)
    assert out["used_llm"] is False
    assert "Gateway total" in out["answer"]


def test_unrecognized_question_does_not_crash():
    rows = [_row("ORD1", STATUS_MATCHED)]
    out = ask("asdkjaslkdjaslkdjas random gibberish", rows)
    assert isinstance(out["answer"], str)


def test_empty_batch_does_not_crash():
    out = ask("What is the match rate?", [])
    assert "0.0%" in out["answer"] or "0 of 0" in out["answer"]


# --------------------------------------------------------------------------
# AI Finance Controller: batch priority analysis
# --------------------------------------------------------------------------

def _row_with_signals(order_ref, status, gw_amount, ld_amount, amount_diff,
                       merchant="Acme Co", reason="test reason"):
    return {
        "order_ref": order_ref, "current_status": status, "status": status,
        "confidence": 0.5, "method": "matched_pair", "reason": reason,
        "signals": {"amount_difference": amount_diff, "date_difference_days": 0},
        "gateway_record": {"merchant": merchant, "amount": str(gw_amount)},
        "ledger_record": {"merchant": merchant, "amount": str(ld_amount)},
    }


def test_batch_analysis_uses_real_counts_no_invention():
    rows = [
        _row("ORD1", STATUS_MATCHED),
        _row("ORD2", STATUS_MATCHED),
        _row_with_signals("ORD3", STATUS_NEEDS_REVIEW, 50000, 5000, 45000),
        _row("ORD4", STATUS_EXCEPTION, method="none"),
    ]
    out = ask("Analyze this batch and tell me what needs my attention.", rows)
    assert out["used_llm"] is False  # always deterministic - never invented
    assert "Total records: 4" in out["answer"]
    assert "Matched: 2" in out["answer"]
    assert "Needs Review: 1" in out["answer"]
    assert "Exceptions: 1" in out["answer"]


def test_batch_analysis_priority_reason_reflects_real_signals():
    rows = [_row_with_signals("ORD3", STATUS_NEEDS_REVIEW, 50000, 5000, 45000)]
    out = ask("What needs my attention in this batch?", rows)
    assert "ORD3" in out["answer"]
    assert "50000" in out["answer"]
    assert "5000" in out["answer"]
    assert "45000" in out["answer"]
    assert "Human review required." in out["answer"]


def test_batch_analysis_missing_ledger_record_recommendation():
    r = _row("ORD5", STATUS_EXCEPTION, method="none")
    r["ledger_record"] = {}
    out = ask("Analyze this batch", [r])
    assert "No matching ledger record found" in out["answer"]
    assert "Investigate ledger entry." in out["answer"]


def test_batch_analysis_reports_nothing_needs_attention_when_all_matched():
    rows = [_row("ORD1", STATUS_MATCHED), _row("ORD2", STATUS_MATCHED)]
    out = ask("Analyze this batch and tell me what needs my attention.", rows)
    assert "No records currently need attention" in out["answer"]
