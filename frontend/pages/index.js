import { useState, useEffect, useCallback } from "react";

const TABS = [
  { key: "EXCEPTION", label: "Exceptions" },
  { key: "NEEDS_REVIEW", label: "Needs Review" },
  { key: "MATCHED", label: "Matched" },
  { key: "all", label: "All Records" },
  { key: "audit", label: "Audit Trail" },
];

const SUGGESTED_QUESTIONS = [
  "What's the total exception amount?",
  "Compare gateway and ledger totals",
  "Which merchant has the highest exception amount?",
];

function money(n) {
  return `Rs ${Number(n || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function StatusPill({ status }) {
  const labelMap = { MATCHED: "Matched", NEEDS_REVIEW: "Needs Review", EXCEPTION: "Exception" };
  const cls = { MATCHED: "matched", NEEDS_REVIEW: "needs_review", EXCEPTION: "exception" }[status] || "";
  return <span className={`status-pill ${cls}`}>{labelMap[status] || status}</span>;
}

function ReviewModal({ result, onClose, onDecide, deciding }) {
  if (!result) return null;
  const gw = result.gateway_record || {};
  const ld = result.ledger_record || {};
  const s = result.signals || {};

  const fields = [
    ["Reference", gw.order_ref, ld.order_ref],
    ["Merchant", gw.merchant, ld.merchant],
    ["Amount", gw.amount ? `Rs ${gw.amount}` : "-", ld.amount ? `Rs ${ld.amount}` : "-"],
    ["Date", gw.date, ld.date],
    ["Narration", gw.narration, ld.narration],
  ];

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{result.order_ref} <StatusPill status={result.current_status} /></h2>
        <div className="modal-sub">
          System recommendation: {result.system_status} · Confidence {result.confidence} · Method: {result.method}
          {result.review && <span className="review-badge">reviewed by {result.review.reviewer}</span>}
        </div>

        <div className="compare-grid">
          <div className="compare-col">
            <h3>Gateway</h3>
            {fields.map(([label, gwVal, ldVal]) => (
              <div key={label} className={`compare-field ${gwVal !== ldVal ? "mismatch" : ""}`}>
                <span>{label}</span><span className="fval">{gwVal ?? "-"}</span>
              </div>
            ))}
          </div>
          <div className="compare-col">
            <h3>Ledger</h3>
            {fields.map(([label, gwVal, ldVal]) => (
              <div key={label} className={`compare-field ${gwVal !== ldVal ? "mismatch" : ""}`}>
                <span>{label}</span><span className="fval">{ldVal ?? "-"}</span>
              </div>
            ))}
          </div>
        </div>

        {Object.keys(s).length > 0 && (
          <div className="signals-grid">
            <div className="signal-card"><div className="slabel">Reference sim.</div><div className="sval">{s.reference_similarity}</div></div>
            <div className="signal-card"><div className="slabel">Merchant sim.</div><div className="sval">{s.merchant_similarity}</div></div>
            <div className="signal-card"><div className="slabel">Narration sim.</div><div className="sval">{s.narration_similarity}</div></div>
            <div className="signal-card"><div className="slabel">Amount diff</div><div className="sval">{s.amount_difference != null ? `Rs${s.amount_difference}` : "-"}</div></div>
            <div className="signal-card"><div className="slabel">Amount diff %</div><div className="sval">{s.amount_difference_pct != null ? `${s.amount_difference_pct}%` : "-"}</div></div>
            <div className="signal-card"><div className="slabel">Date diff (days)</div><div className="sval">{s.date_difference_days ?? "-"}</div></div>
          </div>
        )}

        <div className="reason-text" style={{ maxWidth: "100%", marginBottom: 8 }}>{result.reason}</div>

        <div className="decision-row">
          <button className="btn primary" disabled={deciding} onClick={() => onDecide(result, "confirm_match")}>Confirm Match</button>
          <button className="btn danger" disabled={deciding} onClick={() => onDecide(result, "mark_exception")}>Mark Exception</button>
          <button className="btn" disabled={deciding} onClick={() => onDecide(result, "keep_for_review")}>Keep for Review</button>
          <button className="btn small" onClick={onClose} style={{ marginLeft: "auto" }}>Close</button>
        </div>
      </div>
    </div>
  );
}

function UploadPanel({ onCreated, onCancel }) {
  const [gwFile, setGwFile] = useState(null);
  const [ldFile, setLdFile] = useState(null);
  const [errors, setErrors] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit() {
    if (!gwFile || !ldFile) {
      setErrors(["Select both a gateway CSV and a ledger CSV."]);
      return;
    }
    setBusy(true);
    setErrors(null);
    try {
      const form = new FormData();
      form.append("gateway_file", gwFile);
      form.append("ledger_file", ldFile);
      const res = await fetch("/api/batches", { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = body.detail || {};
        setErrors([
          ...(detail.gateway_errors || []),
          ...(detail.ledger_errors || []),
        ].length ? [...(detail.gateway_errors || []), ...(detail.ledger_errors || [])] : ["Upload failed. Please check the files and try again."]);
        return;
      }
      const batch = await res.json();
      onCreated(batch);
    } catch (e) {
      setErrors([`Could not reach the backend: ${e}`]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="upload-panel">
      <h3 style={{ marginTop: 0, fontSize: 14 }}>Upload a new batch</h3>
      <div className="upload-grid">
        <div className="upload-field">
          <label>Gateway settlements CSV</label>
          <input type="file" accept=".csv" onChange={(e) => setGwFile(e.target.files[0])} />
        </div>
        <div className="upload-field">
          <label>Internal ledger CSV</label>
          <input type="file" accept=".csv" onChange={(e) => setLdFile(e.target.files[0])} />
        </div>
      </div>
      <div className="toolbar" style={{ marginBottom: 0 }}>
        <button className="btn primary" disabled={busy} onClick={handleSubmit}>
          {busy ? "Validating & reconciling…" : "Validate & Reconcile"}
        </button>
        <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
      {errors && (
        <div className="error-box">
          Validation failed — {errors.length} issue(s) found:
          <ul>{errors.slice(0, 10).map((e, i) => <li key={i}>{e}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

export default function Home() {
  const [batch, setBatch] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("EXCEPTION");
  const [showUpload, setShowUpload] = useState(false);
  const [selectedResult, setSelectedResult] = useState(null);
  const [deciding, setDeciding] = useState(false);
  const [toast, setToast] = useState(null);
  const [rerunning, setRerunning] = useState(false);

  const [search, setSearch] = useState("");
  const [amountMismatchOnly, setAmountMismatchOnly] = useState(false);
  const [dateMismatchOnly, setDateMismatchOnly] = useState(false);
  const [auditTrail, setAuditTrail] = useState([]);

  const [question, setQuestion] = useState("");
  const [qaHistory, setQaHistory] = useState([]);
  const [asking, setAsking] = useState(false);

  const loadBatch = useCallback(async (batchId) => {
    try {
      const url = batchId ? `/api/batches/${batchId}` : "/api/reconcile";
      const res = await fetch(url);
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      if (!batchId) {
        // /api/reconcile returns {summary, results, batch_id} - fetch full batch view
        const full = await fetch(`/api/batches/${data.batch_id}`);
        setBatch(await full.json());
      } else {
        setBatch(data);
      }
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { loadBatch(); }, [loadBatch]);

  useEffect(() => {
    if (!batch) return;
    fetch(`/api/audit-trail?batch_id=${batch.id}`)
      .then((r) => r.json())
      .then((d) => setAuditTrail(d.audit_trail || []))
      .catch(() => {});
  }, [batch]);

  function showToast(msg) {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  }

  async function handleDecide(result, decision) {
    setDeciding(true);
    try {
      const res = await fetch(`/api/batches/${batch.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ result_key: result.result_key, decision, reviewer: "operator" }),
      });
      if (!res.ok) throw new Error("Decision failed");
      showToast("Decision recorded and added to the audit trail.");
      setSelectedResult(null);
      await loadBatch(batch.id);
    } catch (e) {
      showToast(`Error: ${e}`);
    } finally {
      setDeciding(false);
    }
  }

  async function handleRerun() {
    setRerunning(true);
    try {
      const res = await fetch(`/api/batches/${batch.id}/rerun`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showToast(body.detail || "Re-run not supported for this batch.");
        return;
      }
      const newBatch = await res.json();
      setBatch(newBatch);
      showToast("Reconciliation re-run — new batch created.");
    } catch (e) {
      showToast(`Error: ${e}`);
    } finally {
      setRerunning(false);
    }
  }

  async function handleAsk(q) {
    const question_ = q || question;
    if (!question_.trim() || !batch) return;
    setAsking(true);
    try {
      const res = await fetch(`/api/ask?batch_id=${batch.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question_ }),
      });
      const json = await res.json();
      setQaHistory((h) => [json, ...h]);
      setQuestion("");
    } catch (e) {
      setQaHistory((h) => [{ question: question_, answer: `Error: ${e}`, used_llm: false, source_records: [] }, ...h]);
    } finally {
      setAsking(false);
    }
  }

  if (error) {
    return (
      <div className="container">
        <div className="empty-state">
          Couldn't reach the backend at /api. Make sure the FastAPI server is running.<br />
          <span style={{ fontSize: 11 }}>{error}</span>
        </div>
      </div>
    );
  }

  if (!batch) {
    return <div className="container"><div className="empty-state">Running reconciliation…</div></div>;
  }

  const { summary, results } = batch;

  let filteredRows = tab === "audit" ? [] : tab === "all" ? results : results.filter((r) => r.current_status === tab);
  if (tab !== "audit") {
    if (search.trim()) {
      const s = search.toLowerCase();
      filteredRows = filteredRows.filter((r) =>
        (r.order_ref || "").toLowerCase().includes(s) ||
        (r.gateway_id || "").toLowerCase().includes(s) ||
        (r.ledger_id || "").toLowerCase().includes(s));
    }
    if (amountMismatchOnly) {
      filteredRows = filteredRows.filter((r) => (r.signals || {}).amount_difference);
    }
    if (dateMismatchOnly) {
      filteredRows = filteredRows.filter((r) => (r.signals || {}).date_difference_days);
    }
  }

  return (
    <div className="container">
      <div className="header">
        <div>
          <h1>SettleSense</h1>
          <div className="tagline">AI Finance Controller — reconciliation, human review &amp; audit trail</div>
        </div>
        <span className="badge">Batch {batch.id} · {new Date(batch.created_at).toLocaleString()}</span>
      </div>

      <div className="toolbar">
        <button className="btn primary" onClick={() => setShowUpload((v) => !v)}>
          {showUpload ? "Close upload" : "Upload New Batch"}
        </button>
        <button className="btn" onClick={handleRerun} disabled={rerunning}>
          {rerunning ? "Re-running…" : "Re-run Reconciliation"}
        </button>
      </div>

      {showUpload && (
        <UploadPanel
          onCancel={() => setShowUpload(false)}
          onCreated={(newBatch) => { setBatch(newBatch); setShowUpload(false); showToast("New batch created."); }}
        />
      )}

      <div className="summary-grid">
        <div className="card"><div className="label">Total Records</div><div className="value">{summary.total_records}</div></div>
        <div className="card"><div className="label">Match Rate</div><div className="value green">{summary.match_rate_pct}%</div></div>
        <div className="card"><div className="label">Needs Review</div><div className="value amber">{summary.needs_review}</div></div>
        <div className="card"><div className="label">Exceptions</div><div className="value red">{summary.exceptions}</div></div>
        <div className="card"><div className="label">Gateway Total</div><div className="value" style={{ fontSize: 16 }}>{money(summary.gateway_total_inr)}</div></div>
        <div className="card"><div className="label">Ledger Total</div><div className="value" style={{ fontSize: 16 }}>{money(summary.ledger_total_inr)}</div></div>
        <div className="card"><div className="label">Gateway - Ledger Diff</div><div className="value amber" style={{ fontSize: 16 }}>{money(summary.gateway_ledger_diff_inr)}</div></div>
        <div className="card"><div className="label">Exception Amount</div><div className="value red" style={{ fontSize: 16 }}>{money(summary.exception_amount_inr)}</div></div>
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <div key={t.key} className={`tab ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>{t.label}</div>
        ))}
      </div>

      {tab !== "audit" && (
        <div className="toolbar">
          <input className="search-input" placeholder="Search order ref, gateway ID, ledger ID…"
                 value={search} onChange={(e) => setSearch(e.target.value)} />
          <span className={`chip-toggle ${amountMismatchOnly ? "active" : ""}`} onClick={() => setAmountMismatchOnly((v) => !v)}>Amount mismatch</span>
          <span className={`chip-toggle ${dateMismatchOnly ? "active" : ""}`} onClick={() => setDateMismatchOnly((v) => !v)}>Date mismatch</span>
        </div>
      )}

      {tab === "audit" ? (
        <div className="panel">
          {auditTrail.length === 0 ? (
            <div className="empty-state">No audit events yet.</div>
          ) : (
            <table>
              <thead><tr><th>Time</th><th>Event</th><th>Order Ref</th><th>Prev → New</th><th>By</th><th>Detail</th></tr></thead>
              <tbody>
                {auditTrail.slice().reverse().map((e, i) => (
                  <tr key={i}>
                    <td style={{ whiteSpace: "nowrap", fontSize: 11 }}>{new Date(e.timestamp).toLocaleString()}</td>
                    <td>{e.event}</td>
                    <td>{e.order_ref || "—"}</td>
                    <td>{e.previous_status ? `${e.previous_status} → ${e.new_status}` : "—"}</td>
                    <td>{e.reviewer}</td>
                    <td className="reason-text">{e.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="panel">
          {filteredRows.length === 0 ? (
            <div className="empty-state">No records in this view.</div>
          ) : (
            <table>
              <thead><tr><th>Order Ref</th><th>Status</th><th>Confidence</th><th>Reason</th></tr></thead>
              <tbody>
                {filteredRows.map((r, i) => (
                  <tr key={i} className="clickable-row" onClick={() => setSelectedResult(r)}>
                    <td>{r.order_ref}</td>
                    <td><StatusPill status={r.current_status} />{r.review && <span className="review-badge">reviewed</span>}</td>
                    <td>{r.confidence}</td>
                    <td className="reason-text">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div className="qa-panel">
        <div className="tabs" style={{ border: "none", marginBottom: 12 }}>
          <div className="tab active" style={{ borderBottom: "none", paddingLeft: 0 }}>Settlement Q&amp;A</div>
        </div>
        <div className="suggested-qs">
          {SUGGESTED_QUESTIONS.map((q) => <button key={q} onClick={() => handleAsk(q)}>{q}</button>)}
        </div>
        <div className="qa-input-row">
          <input placeholder="Ask about this batch…" value={question}
                 onChange={(e) => setQuestion(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && handleAsk()} />
          <button onClick={() => handleAsk()} disabled={asking}>{asking ? "Asking…" : "Ask"}</button>
        </div>
        {qaHistory.map((item, i) => (
          <div className="qa-answer" key={i} style={{ marginBottom: 12 }}>
            <div className="q">{item.question}</div>
            <div>{item.answer}</div>
            <div className="qa-meta">
              Source records: {item.source_records?.join(", ") || "none"} · {item.used_llm ? "LLM-generated" : "template-generated (offline mode)"}
            </div>
          </div>
        ))}
      </div>

      <ReviewModal result={selectedResult} onClose={() => setSelectedResult(null)} onDecide={handleDecide} deciding={deciding} />
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
