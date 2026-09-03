"""
SettleSense — common normalized transaction/settlement structure.

matching.py should never need to know whether a record came from the
shipped synthetic dataset, a Razorpay settlement report, or any other
future source. This module is the single place that translates a
source-specific row (whatever columns/format that source uses) into one
canonical `NormalizedRecord` shape that the reconciliation engine (and
only the reconciliation engine) actually operates on.

Adding a new source later — Razorpay, another gateway, a different ledger
export — means adding one small adapter function here (e.g.
`_normalize_razorpay_settlement_row`) and registering it in
GATEWAY_ADAPTERS / LEDGER_ADAPTERS. It does NOT mean touching matching.py,
its scoring logic, or its thresholds. That's the whole point of this file.

NOT part of this change: no Razorpay adapter is implemented yet. Only the
"synthetic" adapter exists today, mapping the current CSV columns
(settlement_id/entry_id, order_ref, merchant, amount, date, narration)
into the canonical shape. This keeps today's behavior byte-for-byte
identical while making the seam explicit for later.
"""
from __future__ import annotations
import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class NormalizedRecord:
    """One canonical transaction/settlement record, regardless of source.

    Fields are exactly what matching.py's scoring needs (see the five
    signals in matching.py: reference, merchant, narration, amount, date)
    — nothing source-specific leaks in here.
    """
    id: str                      # settlement_id (gateway) or entry_id (ledger)
    order_ref: str
    merchant: str
    narration: str
    amount: Optional[float]      # already parsed; None if missing/invalid
    date: Optional[datetime]     # already parsed; None if missing/invalid
    record_type: str             # "gateway" | "ledger"
    source: str                  # "synthetic" | (future) "razorpay" | ...
    raw: dict                    # original untouched row — preserved verbatim
                                  # for the API/audit-trail contract (gateway_record /
                                  # ledger_record in matching.py's output must stay
                                  # exactly what they are today)


def safe_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def safe_date(x):
    try:
        return datetime.strptime(x, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ---- Source adapters -------------------------------------------------------
# Each adapter maps one source's raw row into a NormalizedRecord. This is the
# ONLY place that should ever know a source's specific field names/formats.

def _normalize_synthetic_gateway_row(raw: dict) -> NormalizedRecord:
    return NormalizedRecord(
        id=raw.get("settlement_id"),
        order_ref=raw.get("order_ref"),
        merchant=raw.get("merchant"),
        narration=raw.get("narration"),
        amount=safe_float(raw.get("amount")),
        date=safe_date(raw.get("date")),
        record_type="gateway",
        source="synthetic",
        raw=raw,
    )


def _normalize_synthetic_ledger_row(raw: dict) -> NormalizedRecord:
    return NormalizedRecord(
        id=raw.get("entry_id"),
        order_ref=raw.get("order_ref"),
        merchant=raw.get("merchant"),
        narration=raw.get("narration"),
        amount=safe_float(raw.get("amount")),
        date=safe_date(raw.get("date")),
        record_type="ledger",
        source="synthetic",
        raw=raw,
    )


# Registries — add a new source by adding one adapter and one entry here.
# e.g. GATEWAY_ADAPTERS["razorpay"] = _normalize_razorpay_settlement_row
GATEWAY_ADAPTERS = {
    "synthetic": _normalize_synthetic_gateway_row,
}
LEDGER_ADAPTERS = {
    "synthetic": _normalize_synthetic_ledger_row,
}


def normalize_gateway_row(raw: dict, source: str = "synthetic") -> NormalizedRecord:
    try:
        adapter = GATEWAY_ADAPTERS[source]
    except KeyError:
        raise ValueError(f"No gateway adapter registered for source '{source}'. "
                          f"Known sources: {list(GATEWAY_ADAPTERS)}")
    return adapter(raw)


def normalize_ledger_row(raw: dict, source: str = "synthetic") -> NormalizedRecord:
    try:
        adapter = LEDGER_ADAPTERS[source]
    except KeyError:
        raise ValueError(f"No ledger adapter registered for source '{source}'. "
                          f"Known sources: {list(LEDGER_ADAPTERS)}")
    return adapter(raw)


def load_gateway_records(path, source: str = "synthetic") -> list[NormalizedRecord]:
    return [normalize_gateway_row(row, source=source) for row in _load_csv(path)]


def load_ledger_records(path, source: str = "synthetic") -> list[NormalizedRecord]:
    return [normalize_ledger_row(row, source=source) for row in _load_csv(path)]