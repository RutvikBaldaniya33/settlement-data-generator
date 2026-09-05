"""
Settlement Q&A Agent.

Retrieval-grounded: every answer is computed from the actual reconciliation
results, never invented. Supports both LLM-backed generation (if an API key
is configured) and a fully offline, deterministic template fallback so the
demo never depends on network access. The reconciliation engine remains the
single source of truth for numbers — the LLM (when used) explains, it does
not calculate aggregates itself, so it can't introduce arithmetic errors.
"""
from __future__ import annotations
import os
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import STATUS_MATCHED, STATUS_NEEDS_REVIEW, STATUS_EXCEPTION


def _row_to_card(r: dict) -> str:
    gw = r.get("gateway_record") or {}
    ld = r.get("ledger_record") or {}
    merchant = gw.get("merchant") or ld.get("merchant") or "unknown merchant"
    amount = gw.get("amount") or ld.get("amount") or "?"
    status = r.get("current_status", r.get("status"))
    return (f"[{r['order_ref']}] status={status} confidence={r['confidence']} "
            f"merchant={merchant} amount=Rs{amount} method={r['method']} "
            f"reason=\"{r['reason']}\"")


def _status_of(r):
    return r.get("current_status", r.get("status"))


def _amt(r):
    gw = r.get("gateway_record") or {}
    ld = r.get("ledger_record") or {}
    try:
        return float(gw.get("amount") or ld.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _merchant_of(r):
    gw = r.get("gateway_record") or {}
    ld = r.get("ledger_record") or {}
    return gw.get("merchant") or ld.get("merchant") or "Unknown"


def _extract_order_refs(question, results):
    refs_in_q = re.findall(r"ORD\d+", question, flags=re.IGNORECASE)
    if not refs_in_q:
        return []
    refs_in_q = {r.upper() for r in refs_in_q}
    return [r for r in results if r["order_ref"].upper() in refs_in_q]


def _extract_number(question, keywords):
    """Pull a numeric threshold near one of the given keywords, e.g.
    'amount mismatch greater than 10' -> 10.0"""
    for kw in keywords:
        m = re.search(rf"{kw}.*?(\d+(?:\.\d+)?)", question, flags=re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _retrieve(question, results, k=10):
    ql = question.lower()
    direct = _extract_order_refs(question, results)
    if direct:
        return direct, "direct_reference"

    if any(kw in ql for kw in ("analyze", "analyse", "attention", "prioritize",
                                "priority", "finance controller")):
        return results, "batch_priority_analysis"

    if "match rate" in ql:
        return results, "match_rate"

    if "amount mismatch" in ql or "amount difference" in ql:
        threshold = _extract_number(ql, ["greater than", "more than", "over", "above", "rs"]) or 0
        subset = [r for r in results
                  if abs((r.get("signals") or {}).get("amount_difference") or 0) > threshold]
        return subset[:k], "amount_mismatch_filter"

    if "date drift" in ql or "date mismatch" in ql or "date difference" in ql:
        threshold = _extract_number(ql, ["greater than", "more than", "over", "above", "days"]) or 0
        subset = [r for r in results
                  if abs((r.get("signals") or {}).get("date_difference_days") or 0) > threshold]
        return subset[:k], "date_mismatch_filter"

    if "low confidence" in ql or "lowest confidence" in ql:
        subset = sorted([r for r in results if r["method"] == "matched_pair"], key=lambda r: r["confidence"])
        return subset[:k], "low_confidence"

    if "highest exception" in ql or ("merchant" in ql and "exception" in ql):
        exc = [r for r in results if _status_of(r) == STATUS_EXCEPTION]
        totals = {}
        for r in exc:
            totals[_merchant_of(r)] = totals.get(_merchant_of(r), 0) + _amt(r)
        if totals:
            top_merchant = max(totals, key=totals.get)
            subset = [r for r in exc if _merchant_of(r) == top_merchant]
            return subset[:k], "merchant_exception_ranking"
        return [], "merchant_exception_ranking"

    # "how many need review", "how much in exception", "how many matched" etc.
    # -> exact status-count questions, answered straight from the data, no
    # silent fallback to unrelated records if the count happens to be zero.
    if "how many" in ql or "how much" in ql:
        if "review" in ql:
            return [r for r in results if _status_of(r) == STATUS_NEEDS_REVIEW], "status_count_review"
        if "exception" in ql:
            return [r for r in results if _status_of(r) == STATUS_EXCEPTION], "status_count_exception"
        if "match" in ql:
            return [r for r in results if _status_of(r) == STATUS_MATCHED], "status_count_matched"

    if "exception" in ql:
        subset = [r for r in results if _status_of(r) == STATUS_EXCEPTION]
        return subset[:k], "status_filter_exception"
    if "review" in ql:
        subset = [r for r in results if _status_of(r) == STATUS_NEEDS_REVIEW]
        return subset[:k], "status_filter_review"
    if "matched" in ql:
        subset = [r for r in results if _status_of(r) == STATUS_MATCHED]
        return subset[:k], "status_filter_matched"

    if "compare" in ql and ("total" in ql or "gateway" in ql and "ledger" in ql):
        return results, "totals_comparison"

    # Fuzzy fallback
    cards = [_row_to_card(r) for r in results]
    if not cards:
        return [], "none"
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit(cards + [question])
    q_vec = vec.transform([question])
    card_vecs = vec.transform(cards)
    sims = cosine_similarity(q_vec, card_vecs)[0]
    top_idx = sims.argsort()[::-1][:k]
    return [results[i] for i in top_idx], "fuzzy_text"


def _compute_aggregates(rows):
    return {
        "count": len(rows),
        "total_amount": round(sum(_amt(r) for r in rows), 2),
        "by_status": {
            s: sum(1 for r in rows if _status_of(r) == s)
            for s in (STATUS_MATCHED, STATUS_NEEDS_REVIEW, STATUS_EXCEPTION)
        },
    }


def _priority_reason(r):
    """Explanation built ONLY from real signals/fields already on the
    record - never invents a number or cause."""
    s = r.get("signals") or {}
    gw = r.get("gateway_record") or {}
    ld = r.get("ledger_record") or {}
    parts = []
    if s.get("amount_difference") not in (None, 0, 0.0) and gw.get("amount") and ld.get("amount"):
        parts.append(f"Gateway: Rs{gw['amount']}, Ledger: Rs{ld['amount']}, "
                      f"Difference: Rs{s['amount_difference']}")
    if not ld:
        parts.append("No matching ledger record found")
    elif not gw:
        parts.append("No matching gateway record found")
    prefix = (" | ".join(parts) + ". ") if parts else ""
    return prefix + (r.get("reason") or "")


def _priority_recommendation(r):
    if _status_of(r) == STATUS_NEEDS_REVIEW:
        return "Human review required."
    if not r.get("ledger_record"):
        return "Investigate ledger entry."
    if not r.get("gateway_record"):
        return "Investigate gateway/settlement entry."
    return "Review this exception manually."


def _batch_priority_analysis_answer(all_results):
    agg = _compute_aggregates(all_results)
    lines = [
        "Batch analyzed.",
        "",
        f"Total records: {agg['count']}",
        f"Matched: {agg['by_status'][STATUS_MATCHED]}",
        f"Needs Review: {agg['by_status'][STATUS_NEEDS_REVIEW]}",
        f"Exceptions: {agg['by_status'][STATUS_EXCEPTION]}",
        "",
    ]
    priority = [r for r in all_results if _status_of(r) in (STATUS_NEEDS_REVIEW, STATUS_EXCEPTION)]
    priority.sort(key=lambda r: abs((r.get("signals") or {}).get("amount_difference") or 0), reverse=True)
    if not priority:
        lines.append("No records currently need attention - everything is matched.")
    else:
        lines.append("Priority items:")
        for i, r in enumerate(priority[:5], 1):
            lines.append(f"\n{i}. {r['order_ref']}")
            lines.append(f"   Reason: {_priority_reason(r)}")
            lines.append(f"   Recommendation: {_priority_recommendation(r)}")
    return "\n".join(lines)


def _template_answer(question, rows, retrieval_mode, all_results):
    if retrieval_mode == "batch_priority_analysis":
        return _batch_priority_analysis_answer(all_results)

    if retrieval_mode == "direct_reference" and rows:
        r = rows[0]
        return (f"{r['order_ref']} is currently **{_status_of(r)}** "
                f"(confidence {r['confidence']}, method: {r['method']}). {r['reason']}")

    if retrieval_mode == "match_rate":
        agg = _compute_aggregates(all_results)
        total = agg["count"]
        matched = agg["by_status"][STATUS_MATCHED]
        rate = round(100 * matched / total, 1) if total else 0.0
        return (f"Match rate: {rate}% ({matched} of {total} records matched). "
                f"Needs review: {agg['by_status'][STATUS_NEEDS_REVIEW]}, "
                f"exceptions: {agg['by_status'][STATUS_EXCEPTION]}.")

    if retrieval_mode in ("status_count_review", "status_count_exception", "status_count_matched"):
        label = {"status_count_review": "need review", "status_count_exception": "are exceptions",
                  "status_count_matched": "are matched"}[retrieval_mode]
        total_amt = round(sum(_amt(r) for r in rows), 2)
        if not rows:
            return f"0 records currently {label} in this batch."
        return f"{len(rows)} record(s) currently {label}, totalling Rs{total_amt:,.2f}."

    if retrieval_mode == "totals_comparison":
        gw_total = round(sum(_amt(r) for r in all_results if r.get("gateway_record")), 2)
        ld_total = round(sum(float((r.get("ledger_record") or {}).get("amount") or 0) for r in all_results), 2)
        return (f"Gateway total: Rs{gw_total:,.2f}. Ledger total: Rs{ld_total:,.2f}. "
                f"Difference: Rs{gw_total - ld_total:,.2f}.")

    if retrieval_mode == "merchant_exception_ranking" and rows:
        merchant = _merchant_of(rows[0])
        total = round(sum(_amt(r) for r in rows), 2)
        return (f"{merchant} has the highest exception amount: Rs{total:,.2f} across "
                f"{len(rows)} record(s).")

    if not rows:
        return "No records match that question in the current batch."

    agg = _compute_aggregates(rows)
    lines = [
        f"Found {agg['count']} relevant record(s), totalling Rs{agg['total_amount']:,.2f}.",
        f"Status breakdown - matched: {agg['by_status'][STATUS_MATCHED]}, "
        f"needs review: {agg['by_status'][STATUS_NEEDS_REVIEW]}, "
        f"exceptions: {agg['by_status'][STATUS_EXCEPTION]}.",
    ]
    for r in rows[:3]:
        lines.append("- " + _row_to_card(r))
    return "\n".join(lines)


def _call_llm(question, context_cards):
    """Returns None if no provider is configured (caller falls back to template).
    The LLM only EXPLAINS the retrieved records - it never computes aggregates
    itself, so it cannot introduce arithmetic errors into the numbers shown."""
    context = "\n".join(context_cards)
    prompt = (
        "You are a settlement reconciliation assistant. Answer ONLY using the "
        "records below - never invent numbers or record IDs that aren't present. "
        "If the records don't contain the answer, say so.\n\n"
        f"Records:\n{context}\n\nQuestion: {question}\nAnswer concisely:"
    )

    if os.environ.get("GEMINI_API_KEY"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            model = genai.GenerativeModel("gemini-2.5-flash")
            return model.generate_content(prompt).text
        except Exception:
            return None

    if os.environ.get("GROQ_API_KEY"):
        try:
            from groq import Groq
            client = Groq(api_key=os.environ["GROQ_API_KEY"])
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content
        except Exception:
            return None

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception:
            return None

    return None


DETERMINISTIC_MODES = {
    "totals_comparison", "match_rate", "batch_priority_analysis",
    "status_count_review", "status_count_exception", "status_count_matched",
}


def ask(question, reconciliation_results):
    retrieved, mode = _retrieve(question, reconciliation_results)

    # Aggregate/count-style questions: compute the number ourselves and never
    # hand raw rows to an LLM to sum/count itself - eliminates arithmetic risk
    # for exactly the questions where a wrong number would be most misleading.
    if mode in DETERMINISTIC_MODES:
        answer = _template_answer(question, retrieved, mode, reconciliation_results)
        used_llm = False
    else:
        llm_answer = _call_llm(question, [_row_to_card(r) for r in retrieved])
        answer = llm_answer if llm_answer else _template_answer(question, retrieved, mode, reconciliation_results)
        used_llm = llm_answer is not None

    return {
        "question": question,
        "answer": answer,
        "used_llm": used_llm,
        "retrieval_mode": mode,
        "source_records": [r["order_ref"] for r in retrieved if r.get("order_ref")],
        "retrieved_count": len(retrieved),
    }
