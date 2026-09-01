# SettleSense

**Razorpay AI Buildathon — Track 04: AI Finance Controller**

An agent that reconciles gateway settlements against internal ledger records, classifies every pair as MATCHED / NEEDS_REVIEW / EXCEPTION with a transparent, multi-signal explanation, lets a human operator review and decide on the ambiguous ones, keeps an immutable audit trail, and answers plain-English questions about the batch — grounded in the actual data, never invented.

Read-only / analysis-only by design. **It never moves money** — no payouts, no refunds, no ledger writes. It classifies and explains; a human (or a separately gated action system) executes.

---

## 1. What SettleSense does

Finance ops teams reconcile settlement records by hand because the join key rarely lines up cleanly: typos in merchant names, truncated references, gateway fees shaving a few rupees off the amount, settlement dates that lag ledger bookings, and — the case that breaks naive reconciliation scripts — a reference ID that matches perfectly while the amount is completely wrong.

SettleSense:
1. Scores every gateway/ledger pair on **five independent signals**, not just "does the reference match."
2. Resolves the whole batch as a **global one-to-one assignment problem** (Hungarian algorithm), so no ledger record is silently claimed by two gateway records.
3. Classifies each pair as **MATCHED / NEEDS_REVIEW / EXCEPTION** by threshold on the combined score — never by an unexplained special case.
4. Gives a human a **side-by-side review workspace** for anything ambiguous, with buttons to confirm, reject, or defer — and never silently auto-corrects on its own.
5. Keeps every event — system classification and human decision alike — in an **audit trail**.
6. Answers questions about the batch through a **Q&A agent** grounded in the actual results; counts and totals are computed by the engine, never estimated by an LLM.

---

## 2. Problem statement (why exact-reference matching is not enough)

A naive reconciliation script joins on `order_ref` and calls it done. This fails, or worse, silently "succeeds" incorrectly, on cases like:

> Gateway: `ORD10001`, ₹50,000
> Ledger: `ORD10001`, ₹5,000

Same reference — but a 10x amount mismatch. A naive script marks this **MATCHED**. SettleSense does not: the reference signal contributes its configured weight (30%) like every other signal, so a wrong amount drags the combined confidence down and the pair lands in **NEEDS_REVIEW** or **EXCEPTION** instead. This exact scenario is covered by `test_exact_reference_but_huge_amount_mismatch_is_not_auto_matched` in the test suite.

---

## 3. Architecture

```
backend/
  app/
    config.py       every threshold & weight, with reasoning inline
    data_gen.py      synthetic gateway + ledger data generator with
                     deliberately injected mismatch scenarios (see §7)
    matching.py      the reconciliation engine (see §4)
    validation.py    CSV upload validation
    store.py         in-memory batch + audit-trail store (see §8, limitation)
    qa_agent.py       retrieval-grounded Q&A agent (see §6)
    main.py          FastAPI app — all endpoints (see §9)
  tests/
    test_matching.py    11 tests — scoring, assignment, edge cases
    test_qa_agent.py     7 tests — deterministic answers, no-hallucination
    test_validation.py   9 tests — CSV validation, malformed input
  data/              generated CSVs
  requirements.txt

frontend/
  pages/index.js     dashboard: upload, KPIs, filterable record tables,
                      human review modal, audit trail tab, Q&A chat
  styles/globals.css
```

---

## 4. Matching methodology

Every gateway/ledger pair is scored on five signals, each 0.0–1.0:

| Signal | What it measures | Method |
|---|---|---|
| `reference` | order reference similarity | character n-gram TF-IDF cosine (robust to single-char typos) |
| `merchant` | merchant name similarity | same |
| `narration` | free-text description similarity | same |
| `amount` | amount closeness | tolerance band (₹20 absolute or 0.5% relative, whichever larger) scoring 1.0, decaying linearly to 0.0 by 5x that tolerance |
| `date` | settlement vs. booking date closeness | 4-day tolerance window scoring 1.0, decaying linearly to 0.0 by 3x that window |

**Confidence** = `0.30·reference + 0.15·merchant + 0.15·narration + 0.25·amount + 0.15·date`. Weights and tolerances live in `config.py`. An exact reference match alone contributes only 0.30 of the 0.85 auto-match bar — it cannot force a MATCHED status by itself.

**Assignment**: pairwise scores form a gateway x ledger matrix, resolved by `scipy.optimize.linear_sum_assignment` (Hungarian algorithm) — the globally optimal one-to-one pairing, not a greedy first-best loop. Pairs below `CANDIDATE_FLOOR` (0.35) are excluded before assignment.

**Status**: `>= 0.85` MATCHED, `>= 0.55` NEEDS_REVIEW, below that EXCEPTION. No other code path assigns status.

**Accurate language**: an unmatched record gets *"No eligible ledger candidate was found within the configured matching constraints... Possible causes: missing ledger booking, incorrect reference, or a mismatch beyond configured tolerances"* — stating what the engine can prove, not asserting facts (like "never booked internally") it has no way to verify.

---

## 5. Human review workflow

Any `NEEDS_REVIEW` record opens a comparison view: gateway vs. ledger record side by side with mismatched fields highlighted, the full signal breakdown, and the system's reasoning. The operator picks **Confirm Match**, **Mark Exception**, or **Keep for Review** — this only records a decision, never a financial action. `system_status` (original recommendation) is preserved forever alongside `current_status` (post-review).

---

## 6. Q&A agent

Supports, all verified in `test_qa_agent.py`:
- Direct record lookup: *"Why is ORD10056 an exception?"*
- Exact aggregate answers computed by the engine: *"What's the match rate?"*, *"How many need review?"*, *"How much is in exception?"* — including the honest zero-count case ("0 records currently need review") instead of silently substituting unrelated records.
- Filtered queries: amount mismatch thresholds, date drift thresholds, highest-exception merchant, low-confidence matches.
- Free-form fallback via TF-IDF similarity.

LLM usage (Gemini/Groq/Claude, via env var) is optional and bounded — it phrases answers from pre-retrieved records, but count/rate/total questions are always computed by the engine, never summed by the model, so they can't be arithmetically wrong. Every answer reports `source_records` and `used_llm` so provenance is never hidden.

---

## 7. Demo dataset

`data_gen.py` generates ~60 gateway + ~63 ledger records with: clean matches, typo'd references/merchants/narrations, truncated references, gateway-fee amount deltas, date drift, a stacked low-confidence scenario, a duplicate ledger booking, and orphan records on both sides. Result on a generated batch: **78.1% match rate**, ~1 needing review, ~13 genuine exceptions — deliberately not 100%.

---

## 8. Batch model & audit trail

Each run is a **batch** (`BATCH-YYYYMMDD-HHMMSS-xxxxxx`) storing source filenames, counts, results, and a live summary reflecting human decisions. The **audit trail** is an append-only log of every event (system classification and human decision alike) with timestamp, IDs, previous/new status, system recommendation, human decision, and reason.

**Known limitation**: the store is an in-memory Python dict — state resets on restart. This is a documented MVP simplification; swapping in SQLite/Postgres would keep the same shape.

---

## 9. API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness |
| POST | `/api/validate?kind=gateway\|ledger` | validate a CSV before committing |
| POST | `/api/batches` | upload CSVs → validate → reconcile → create batch |
| GET | `/api/batches` / `/api/batches/{id}` | list / detail |
| POST | `/api/batches/{id}/rerun` | re-run as a new batch (never mutates the old one) |
| GET | `/api/reconcile` | latest batch's summary + results |
| GET | `/api/results` | search & filter: `status`, `search`, `merchant`, `min_confidence`, `max_confidence`, `amount_mismatch_only`, `date_mismatch_only` |
| GET | `/api/exceptions` / `/api/needs-review` | status shortcuts |
| POST | `/api/batches/{id}/review` | human decision |
| GET | `/api/audit-trail` | full log |
| POST | `/api/ask` | Q&A |

All unhandled exceptions are caught globally and returned as a generic message — no raw Python traceback ever reaches the client.

---

## 10. Running it

**Backend:**
```bash
cd backend
pip install -r requirements.txt
python app/data_gen.py
uvicorn app.main:app --reload --port 8000
```

**Tests:** `cd backend && python -m pytest tests/ -v` (27 tests, all passing)

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:3000`.

**Optional LLM for Q&A** (works correctly without one): `export GEMINI_API_KEY=...` (or `GROQ_API_KEY` / `ANTHROPIC_API_KEY`)

---

## 11. What broke, and how I got out of it

1. **Fuzzy matching was never actually exercised.** The first data generator kept `order_ref` identical across every mismatch scenario, so every "hard" case still exact-matched on the join key. Fixed by corrupting the join key itself in typo/partial-ref scenarios and switching to character n-gram TF-IDF, robust to single-character typos.

2. **Q&A gave a wrong-shaped answer on a zero count.** After a review pushed all `NEEDS_REVIEW` records elsewhere, *"how many need review?"* fell through to a generic top-10 fallback and returned unrelated records instead of saying "0." Fixed with explicit deterministic handling for count/rate questions, plus a regression test so it can't silently reappear.

---

## 12. Honest limitations

- In-memory store — batches and audit trail are lost on restart.
- Synthetic data, not live Razorpay test-mode settlements.
- Thresholds tuned by inspection, not cross-validated against a larger labeled set.
- Re-run is only implemented for the default demo dataset; an uploaded batch is re-run by re-uploading.
- Q&A's free-form fallback (TF-IDF) is weaker than the pattern-matched paths for genuinely novel multi-hop questions.
- No auth on any endpoint; CORS is wide open — fine for a local demo, flagged inline as a pre-deployment TODO.

## Recommended next steps after MVP

- Move `store.py` to SQLite/Postgres — same shape, different persistence layer.
- Add auth + real reviewer identity (the audit trail's `reviewer` field is ready for it).
- Cross-validate matching thresholds against a larger, ideally real, labeled dataset.
- Extend rerun to support uploaded batches by persisting the original files.
