"""
SettleSense — common normalized transaction/settlement structure.

matching.py should never need to know whether a record came from the
shipped synthetic dataset, a Razorpay payment, or any other future
source. This module is the single place that translates a
source-specific row (whatever columns/format that source uses) into one
canonical `NormalizedRecord` shape that the reconciliation engine (and
only the reconciliation engine) actually operates on.

Currently supported adapters:
  - "synthetic" — maps the shipped CSV columns (settlement_id/entry_id,
    order_ref, merchant, amount, date, narration) into the canonical
    shape.
  - "razorpay" — maps a raw Razorpay Payment object (as fetched via the
    dedicated Razorpay client module) into the canonical shape. Razorpay
    payment normalization is implemented; fetching/reconciling that data
    end-to-end is a separate, later concern.

Adding another new source later — a different gateway, a different
ledger export — means adding one small adapter function here and
registering it in GATEWAY_ADAPTERS / LEDGER_ADAPTERS. It does NOT mean
touching matching.py, its scoring logic, or its thresholds. That's the
whole point of this file.
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


def _normalize_razorpay_payment_row(raw: dict) -> NormalizedRecord:
    """Maps one raw Razorpay Payment object (as returned by the Razorpay API
    via the dedicated Razorpay client module) into the canonical
    NormalizedRecord shape.

    This function only transforms an already-fetched dict; it makes no API
    calls itself and never touches credentials.

    Note on `merchant`: a Razorpay Payment object has no field that
    identifies a merchant (the payment already belongs to your own
    account) - the only per-payment identifier close to it is the
    customer's `email`, which is sensitive customer PII, not merchant
    information. Rather than invent a merchant name or expose that PII
    unnecessarily, this leaves `merchant` empty for Razorpay records.
    """
    raw_amount = safe_float(raw.get("amount"))
    amount = round(raw_amount / 100, 2) if raw_amount is not None else None

    date = None
    created_at = raw.get("created_at")
    if created_at is not None:
        try:
            date = datetime.fromtimestamp(float(created_at))
        except (TypeError, ValueError, OSError, OverflowError):
            date = None

    return NormalizedRecord(
        id=raw.get("id", ""),
        order_ref=raw.get("order_id") or "",
        merchant="",  # see docstring - no non-sensitive merchant field exists
        narration=raw.get("description") or "",
        amount=amount,
        date=date,
        record_type="gateway",
        source="razorpay",
        raw=raw,
    )


# Registries — "synthetic" and "razorpay" are registered below. Add another
# source later by adding one adapter function and one entry here.
GATEWAY_ADAPTERS = {
    "synthetic": _normalize_synthetic_gateway_row,
    "razorpay": _normalize_razorpay_payment_row,
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
