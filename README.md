# SettleSense

### Razorpay AI Buildathon — Track 04: AI Finance Controller

SettleSense is an AI-assisted finance reconciliation system that compares payment gateway records with an internal ledger and helps finance teams find mismatches quickly.

It does not move money, make payments, issue refunds, or modify financial records.

It only:
- compares records
- finds mismatches
- explains why records do not match
- allows a human to review uncertain cases
- keeps an audit trail
- answers questions about the reconciliation batch

---

## What Problem Does SettleSense Solve?

Finance teams often need to compare two sources of financial data:

**Payment Gateway**

```text
Gateway
   ↓
Payment / Settlement Records

Internal Ledger



Internal System
   ↓
Ledger Records

The same transaction may not look exactly the same in both systems.

For example:



Gateway
Order: ORD10001
Amount: ₹50,000

Ledger
Order: ORD10001
Amount: ₹5,000

The order reference is the same, but the amount is completely different.

A simple system that only checks the order reference could incorrectly mark this as a match.

SettleSense looks at multiple signals before making a decision.

How SettleSense Works



Gateway Records
       │
       ▼
   Normalize
       │
       ▼
Internal Ledger ──► Normalize
       │
       ▼
 Matching Engine
       │
       ├── MATCHED
       ├── NEEDS_REVIEW
       └── EXCEPTION
       │
       ▼
 Human Review
       │
       ▼
   Audit Trail
       │
       ▼
 Q&A / Dashboard

The system can work with demo CSV data and can also fetch payment data from the Razorpay TEST API.

Matching Engine

SettleSense does not depend on one field.

Each gateway/ledger pair is evaluated using five signals:

Signal

What it checks

Reference

Order/payment reference similarity

Merchant

Merchant name similarity

Narration

Description/text similarity

Amount

Difference between amounts

Date

Difference between settlement and booking dates

The signals are combined into a confidence score:



Confidence =
0.30 × Reference
+ 0.15 × Merchant
+ 0.15 × Narration
+ 0.25 × Amount
+ 0.15 × Date

The weights and matching thresholds are configurable in config.py.

An exact reference match is not enough to automatically become a match.

One-to-One Matching

After calculating pairwise scores, SettleSense solves the complete matching problem using the Hungarian algorithm.

This makes the assignment global and one-to-one.

In simple terms:



Gateway A ───── Ledger 1
Gateway B ───── Ledger 2
Gateway C ───── Ledger 3

A single ledger record cannot silently be assigned to multiple gateway records.

Low-confidence candidates are removed before assignment.

Result Classification

Every record receives one of three statuses:



MATCHED
NEEDS_REVIEW
EXCEPTION

Current thresholds:

ConfidenceStatus



≥ 0.85

MATCHED

≥ 0.55

NEEDS_REVIEW

< 0.55

EXCEPTION

These thresholds are defined in the backend configuration.

Human Review

Not every reconciliation decision should be automated.

When a record needs review, the dashboard shows:

Gateway record

Ledger record

Mismatched fields

Signal scores

Confidence

System reasoning

The reviewer can choose:



Confirm Match
Mark Exception
Keep for Review

Human review only changes the reconciliation status.

It does not perform any financial action.

The original system recommendation is preserved as system_status, while the current decision is stored as current_status.

Audit Trail

Every important action is recorded in an append-only audit trail.

Examples:



batch_created
system_classification
human_review

The audit information includes:

timestamp

batch ID

record ID

previous status

new status

system recommendation

human decision

reason

This makes it possible to understand how a reconciliation result was produced.

Q&A

SettleSense includes a Q&A interface for asking questions about the current reconciliation batch.

Examples:



What's the match rate?

How many records need review?

How much money is in exceptions?

Why is ORD10056 an exception?

Important design decision:

The system does not ask an LLM to calculate financial totals.

Counts, rates and totals are calculated directly from the reconciliation results.

An optional LLM can be used to explain retrieved information in natural language.

The Q&A response also keeps track of:



source_records
used_llm

so the answer has clear provenance.

Razorpay TEST Integration

SettleSense can connect to the Razorpay TEST API.

The integration:

Connects using Razorpay TEST credentials

Fetches payment records

Normalizes the Razorpay response

Converts payment amounts into the internal amount format

Reconciles the data against the internal ledger

Creates a normal SettleSense batch

Shows the results in the dashboard

Records the batch in the audit trail

The integration uses environment variables:



RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET

Only Razorpay TEST keys are accepted.

Live Razorpay keys are intentionally rejected.

Never commit .env or real credentials to GitHub.

Dashboard

The dashboard provides:

Total Records

Match Rate

Needs Review count

Exception count

Gateway Total

Ledger Total

Gateway/Ledger Difference

Exception Amount

It also provides tabs for:



Exceptions
Needs Review
Matched
All Records
Audit Trail

Records can be searched and filtered by different mismatch conditions.

Project Structure



settlesense/
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── matching.py
│   │   ├── normalize.py
│   │   ├── config.py
│   │   ├── qa_agent.py
│   │   ├── data_gen.py
│   │   ├── razorpay_client.py
│   │   └── store.py
│   │
│   ├── data/
│   │   ├── gateway_settlements.csv
│   │   └── internal_ledger.csv
│   │
│   ├── tests/
│   │   ├── test_matching.py
│   │   ├── test_matching_scenarios.py
│   │   ├── test_qa_agent.py
│   │   ├── test_validation.py
│   │   ├── test_razorpay_client.py
│   │   └── test_razorpay_reconcile.py
│   │
│   └── requirements.txt
│
├── frontend/
│   ├── pages/
│   │   ├── _app.js
│   │   └── index.js
│   │
│   ├── styles/
│   │   └── globals.css
│   │
│   ├── package.json
│   └── next.config.js
│
├── .env.example
├── .gitignore
└── README.md

API

Main API endpoints:

Method

Endpoint

Purpose

GET

/api/health

Check backend health

POST

/api/validate

Validate gateway/ledger CSV

POST

/api/batches

Upload and reconcile CSV files

GET

/api/batches

List batches

GET

/api/batches/{id}

Get batch details

POST

/api/batches/{id}/rerun

Re-run reconciliation

GET

/api/reconcile

Get latest reconciliation

GET

/api/results

Search and filter results

GET

/api/exceptions

Get exception records

GET

/api/needs-review

Get records needing review

POST

/api/batches/{id}/review

Submit human review

GET

/api/audit-trail

Get audit events

POST

/api/ask

Ask questions about the batch

GET

/api/razorpay/reconcile

Fetch Razorpay TEST payments and reconcile

Running the Project

1. Backend

Open PowerShell:



cd backend
pip install -r requirements.txt
python app/data_gen.py
uvicorn app.main:app --reload --port 8000

Backend:



http://localhost:8000

API documentation:



http://localhost:8000/docs

2. Frontend

Open another terminal:



cd frontend
npm install
npm run dev

Frontend:



http://localhost:3000

Razorpay TEST Setup

Create a local file:



backend/.env

Add:



RAZORPAY_KEY_ID=your_test_key_id
RAZORPAY_KEY_SECRET=your_test_key_secret

Do not commit this file.

The project .gitignore already excludes:



.env

Then start the backend and use the Razorpay TEST button from the dashboard.

Running Tests

The project currently has:



84 backend tests

Run:



cd backend
pytest -q

Expected result:



84 passed

The test suite covers areas including:

matching logic

reconciliation scenarios

validation

Q&A

Razorpay client

Razorpay reconciliation

batch persistence

error handling

credential protection

Demo Data

The project includes a synthetic data generator.

The generated data intentionally contains different reconciliation scenarios such as:

clean matches

reference typos

merchant differences

narration differences

truncated references

amount differences

date drift

duplicate ledger records

unmatched gateway records

unmatched ledger records

low-confidence cases

The demo dataset is intentionally imperfect so that the reconciliation workflow can be demonstrated properly.

Security

SettleSense is designed as an analysis/reconciliation MVP.

Important security decisions:

Razorpay TEST credentials are loaded from environment variables.

Live Razorpay keys are rejected.

Credentials are not returned in API responses.

.env is ignored by Git.

Raw backend tracebacks are not returned to clients.

This project is not intended to be deployed directly to production without additional security controls.

Limitations

This is an MVP, so there are some known limitations:

Batch storage currently uses an in-memory Python store.

Data and audit history are lost when the backend restarts.

The demo dataset is synthetic.

Matching thresholds have not been trained on a large labelled production dataset.

Authentication is not implemented.

CORS is currently configured for the MVP/demo environment.

Re-run support is focused on the demo/default dataset.

Q&A fallback is limited for completely new multi-step questions.

Future Improvements

Possible next steps:

Replace the in-memory store with SQLite/PostgreSQL.

Add authentication and reviewer identity.

Improve threshold calibration using a larger labelled dataset.

Persist uploaded batches for complete re-run support.

Add stronger production security controls.

Expand integrations with additional payment gateways.

Design Principle

The main principle behind SettleSense is:

AI should help finance teams understand reconciliation results, not silently make financial decisions.

The system therefore separates:



Data
  ↓
Matching
  ↓
Classification
  ↓
Explanation
  ↓
Human Review
  ↓
Audit Trail

Financial actions remain outside the system.

Current Project Status



Core reconciliation       ✅
Multi-signal matching     ✅
Hungarian assignment      ✅
Synthetic CSV workflow    ✅
Dashboard                 ✅
Human review              ✅
Audit trail               ✅
Q&A                       ✅
Razorpay TEST API         ✅
Razorpay normalization    ✅
Razorpay reconciliation   ✅
Batch persistence         ✅
Backend tests             ✅ 84/84
Frontend build            ✅