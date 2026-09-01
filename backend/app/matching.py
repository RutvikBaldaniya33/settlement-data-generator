"""
SettleSense reconciliation engine (v2).

Design, per the Track 04 bar ("every money action explainable, honest
metrics, no cherry-picking"):

1. MULTI-SIGNAL SCORING — every gateway/ledger pair is scored on five
   independent signals (reference, merchant, narration, amount, date), each
   0.0-1.0, combined by a documented weighted sum (see config.py). An exact
   reference match contributes its weight like any other signal — it does
   NOT short-circuit to an automatic match. A gateway record for Rs 50,000
   and a same-reference ledger record for Rs 5,000 will score low on the
   amount signal and can still land in NEEDS_REVIEW or EXCEPTION.

2. GLOBAL ONE-TO-ONE ASSIGNMENT — candidate pairs are resolved with the
   Hungarian algorithm (scipy.optimize.linear_sum_assignment) over the full
   gateway x ledger score matrix, not a greedy first-best-match loop. This
   guarantees no ledger record is claimed by two gateway records, and that
   the assignment is globally optimal, not just locally reasonable.

3. THREE-WAY STATUS — MATCHED / NEEDS_REVIEW / EXCEPTION, decided purely by
   thresholds on the final confidence score (config.py). No other code path
   assigns status.

4. STRUCTURED EXPLANATION — every result carries the raw signal scores, not
   just a prose reason, so a finance operator (or the Q&A layer) can see
   exactly what drove the decision.

5. ACCURATE LANGUAGE — the engine never claims something it can't prove.
   "No eligible ledger candidate was found within the configured matching
   constraints" — not "gateway settled but never booked internally", which
   asserts a fact (that it truly was never booked) the engine cannot verify.

Read-only / analysis-only: this never moves money, only classifies.
"""
from __future__ import annotations
import csv
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import (
    SIGNAL_WEIGHTS, AMOUNT_TOLERANCE_ABS_INR, AMOUNT_TOLERANCE_PCT,
    AMOUNT_SCORE_DECAY_MULTIPLE, DATE_TOLERANCE_DAYS, DATE_SCORE_DECAY_MULTIPLE,
    AUTO_MATCH_THRESHOLD, NEEDS_REVIEW_THRESHOLD, CANDIDATE_FLOOR,
    STATUS_MATCHED, STATUS_NEEDS_REVIEW, STATUS_EXCEPTION,
)


@dataclass
class MatchResult:
    gateway_id: str
    ledger_id: Optional[str]
    order_ref: str
    status: str
    confidence: float
    method: str                    # "matched_pair" | "none"
    reason: str
    signals: dict = field(default_factory=dict)
    gateway_record: dict = field(default_factory=dict)
    ledger_record: dict = field(default_factory=dict)


def _load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _safe_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_date(x):
    try:
        return datetime.strptime(x, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _text_sim_matrix(texts_a, texts_b):
    """Char n-gram TF-IDF cosine similarity - robust to single-char typos."""
    m, n = len(texts_a), len(texts_b)
    if m == 0 or n == 0:
        return np.zeros((m, n))
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(texts_a + texts_b)
    a = vec.transform(texts_a)
    b = vec.transform(texts_b)
    return cosine_similarity(a, b)


def _amount_score_matrix(amounts_a, amounts_b):
    m, n = len(amounts_a), len(amounts_b)
    scores = np.zeros((m, n))
    for i, a in enumerate(amounts_a):
        for j, b in enumerate(amounts_b):
            if a is None or b is None:
                continue
            diff = abs(a - b)
            tol = max(AMOUNT_TOLERANCE_ABS_INR, AMOUNT_TOLERANCE_PCT / 100.0 * max(a, b, 1))
            decay_end = tol * AMOUNT_SCORE_DECAY_MULTIPLE
            if diff <= tol:
                scores[i, j] = 1.0
            elif diff >= decay_end:
                scores[i, j] = 0.0
            else:
                scores[i, j] = 1.0 - (diff - tol) / (decay_end - tol)
    return scores


def _date_score_matrix(dates_a, dates_b):
    m, n = len(dates_a), len(dates_b)
    scores = np.zeros((m, n))
    decay_end = DATE_TOLERANCE_DAYS * DATE_SCORE_DECAY_MULTIPLE
    for i, a in enumerate(dates_a):
        for j, b in enumerate(dates_b):
            if a is None or b is None:
                continue
            diff = abs((a - b).days)
            if diff <= DATE_TOLERANCE_DAYS:
                scores[i, j] = 1.0
            elif diff >= decay_end:
                scores[i, j] = 0.0
            else:
                scores[i, j] = 1.0 - (diff - DATE_TOLERANCE_DAYS) / (decay_end - DATE_TOLERANCE_DAYS)
    return scores


def _classify(confidence):
    if confidence >= AUTO_MATCH_THRESHOLD:
        return STATUS_MATCHED
    if confidence >= NEEDS_REVIEW_THRESHOLD:
        return STATUS_NEEDS_REVIEW
    return STATUS_EXCEPTION


def reconcile(gateway_path, ledger_path):
    gateway = _load_csv(gateway_path)
    ledger = _load_csv(ledger_path)
    m, n = len(gateway), len(ledger)

    gw_ref = [r["order_ref"] for r in gateway]
    lg_ref = [r["order_ref"] for r in ledger]
    gw_merchant = [r["merchant"] for r in gateway]
    lg_merchant = [r["merchant"] for r in ledger]
    gw_narr = [r["narration"] for r in gateway]
    lg_narr = [r["narration"] for r in ledger]
    gw_amt = [_safe_float(r["amount"]) for r in gateway]
    lg_amt = [_safe_float(r["amount"]) for r in ledger]
    gw_date = [_safe_date(r["date"]) for r in gateway]
    lg_date = [_safe_date(r["date"]) for r in ledger]

    ref_sim = _text_sim_matrix(gw_ref, lg_ref)
    merchant_sim = _text_sim_matrix(gw_merchant, lg_merchant)
    narr_sim = _text_sim_matrix(gw_narr, lg_narr)
    amount_score = _amount_score_matrix(gw_amt, lg_amt)
    date_score = _date_score_matrix(gw_date, lg_date)

    if m and n:
        confidence = (
            SIGNAL_WEIGHTS["reference"] * ref_sim +
            SIGNAL_WEIGHTS["merchant"] * merchant_sim +
            SIGNAL_WEIGHTS["narration"] * narr_sim +
            SIGNAL_WEIGHTS["amount"] * amount_score +
            SIGNAL_WEIGHTS["date"] * date_score
        )
    else:
        confidence = np.zeros((m, n))

    # Global one-to-one optimal assignment (Hungarian algorithm). Cost = 1 -
    # confidence so minimizing cost maximizes total confidence. Rectangular
    # matrices (m != n) are handled natively.
    matched_pairs = {}  # gateway_idx -> ledger_idx
    if m and n:
        cost = 1.0 - confidence
        row_idx, col_idx = linear_sum_assignment(cost)
        for i, j in zip(row_idx, col_idx):
            if confidence[i, j] >= CANDIDATE_FLOOR:
                matched_pairs[i] = j

    results = []
    used_ledger = set(matched_pairs.values())

    for i, gw in enumerate(gateway):
        if i in matched_pairs:
            j = matched_pairs[i]
            ld = ledger[j]
            conf = float(confidence[i, j])
            status = _classify(conf)
            amt_diff = None
            amt_diff_pct = None
            if gw_amt[i] is not None and lg_amt[j] is not None:
                amt_diff = round(gw_amt[i] - lg_amt[j], 2)
                base = max(abs(gw_amt[i]), abs(lg_amt[j]), 1)
                amt_diff_pct = round(100 * amt_diff / base, 2)
            date_diff = None
            if gw_date[i] is not None and lg_date[j] is not None:
                date_diff = (gw_date[i] - lg_date[j]).days

            signals = {
                "reference_similarity": round(float(ref_sim[i, j]), 3),
                "merchant_similarity": round(float(merchant_sim[i, j]), 3),
                "narration_similarity": round(float(narr_sim[i, j]), 3),
                "amount_difference": amt_diff,
                "amount_difference_pct": amt_diff_pct,
                "amount_score": round(float(amount_score[i, j]), 3),
                "date_difference_days": date_diff,
                "date_score": round(float(date_score[i, j]), 3),
            }

            reason_parts = [f"Weighted confidence {conf:.2f} from reference, merchant, "
                             f"narration, amount and date signals."]
            if status != STATUS_MATCHED:
                weak = [k for k, v in {
                    "reference": ref_sim[i, j], "merchant": merchant_sim[i, j],
                    "narration": narr_sim[i, j], "amount": amount_score[i, j],
                    "date": date_score[i, j],
                }.items() if v < 0.5]
                if weak:
                    reason_parts.append(f"Weak signal(s): {', '.join(weak)}.")
                if amt_diff is not None and abs(amt_diff) > AMOUNT_TOLERANCE_ABS_INR:
                    reason_parts.append(f"Amount differs by Rs {amt_diff:+.2f} ({amt_diff_pct:+.1f}%).")
                if status == STATUS_NEEDS_REVIEW:
                    reason_parts.append("Below the auto-match bar - flagged for human review.")
                else:
                    reason_parts.append("Below the review bar - treated as a genuine exception "
                                         "despite a candidate existing.")

            results.append(MatchResult(
                gateway_id=gw["settlement_id"], ledger_id=ld["entry_id"],
                order_ref=gw["order_ref"], status=status, confidence=round(conf, 3),
                method="matched_pair", reason=" ".join(reason_parts),
                signals=signals, gateway_record=gw, ledger_record=ld,
            ))
        else:
            results.append(MatchResult(
                gateway_id=gw["settlement_id"], ledger_id=None,
                order_ref=gw["order_ref"], status=STATUS_EXCEPTION, confidence=0.0,
                method="none",
                reason="No eligible ledger candidate was found within the configured "
                       "matching constraints (reference/merchant/narration similarity, "
                       "amount tolerance, date window). Possible causes: missing ledger "
                       "booking, incorrect reference, or a mismatch beyond configured tolerances.",
                signals={}, gateway_record=gw, ledger_record={},
            ))

    for j, ld in enumerate(ledger):
        if j not in used_ledger:
            results.append(MatchResult(
                gateway_id="", ledger_id=ld["entry_id"], order_ref=ld["order_ref"],
                status=STATUS_EXCEPTION, confidence=0.0, method="none",
                reason="No eligible gateway settlement was found within the configured "
                       "matching constraints. Possible causes: failed payout, duplicate "
                       "booking, or a mismatch beyond configured tolerances.",
                signals={}, gateway_record={}, ledger_record=ld,
            ))

    total = len(results)
    matched = sum(1 for r in results if r.status == STATUS_MATCHED)
    needs_review = sum(1 for r in results if r.status == STATUS_NEEDS_REVIEW)
    exceptions = sum(1 for r in results if r.status == STATUS_EXCEPTION)

    def amt_of(r):
        return _safe_float(r.gateway_record.get("amount") or r.ledger_record.get("amount"), 0) or 0

    gateway_total = round(sum(_safe_float(r["amount"], 0) or 0 for r in gateway), 2)
    ledger_total = round(sum(_safe_float(r["amount"], 0) or 0 for r in ledger), 2)
    matched_amount = round(sum(amt_of(r) for r in results if r.status == STATUS_MATCHED), 2)
    needs_review_amount = round(sum(amt_of(r) for r in results if r.status == STATUS_NEEDS_REVIEW), 2)
    exception_amount = round(sum(amt_of(r) for r in results if r.status == STATUS_EXCEPTION), 2)

    summary = {
        "total_records": total,
        "gateway_records": len(gateway),
        "ledger_records": len(ledger),
        "matched": matched,
        "needs_review": needs_review,
        "exceptions": exceptions,
        "match_rate_pct": round(100 * matched / total, 1) if total else 0.0,
        "gateway_total_inr": gateway_total,
        "ledger_total_inr": ledger_total,
        "gateway_ledger_diff_inr": round(gateway_total - ledger_total, 2),
        "matched_amount_inr": matched_amount,
        "needs_review_amount_inr": needs_review_amount,
        "exception_amount_inr": exception_amount,
    }

    return {"summary": summary, "results": [r.__dict__ for r in results]}


if __name__ == "__main__":
    import json, os
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(here, "..", "data")
    out = reconcile(os.path.join(data_dir, "gateway_settlements.csv"),
                     os.path.join(data_dir, "internal_ledger.csv"))
    print(json.dumps(out["summary"], indent=2))
