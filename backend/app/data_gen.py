"""
Generates synthetic 'gateway settlement' records (like a Razorpay settlement
report) and 'internal ledger' records (like a merchant's own bookkeeping),
deliberately injecting realistic real-world mismatches so the reconciliation
engine has a genuine problem to solve — not a toy exact-match dataset.

Mismatch types injected (mirrors what actually happens in finance ops):
  1. Clean matches           - reference IDs line up exactly
  2. Typo'd merchant/narration text - same transaction, human-entered text differs
  3. Partial/truncated reference numbers
  4. Amount off by rounding / currency fee deduction (₹2-15 delta)
  5. Date drift (settlement lags ledger entry by 1-3 days)
  6. Orphan gateway records  - settled by gateway, never booked internally
  7. Orphan ledger records   - booked internally, gateway never paid out (true exception)
"""
import csv
import random
from datetime import datetime, timedelta

random.seed(42)

MERCHANTS = [
    "Sharma Textiles Pvt Ltd", "Bardoli Agro Traders", "Surat Silk House",
    "Kiran Electronics", "Patel Auto Parts", "Sunrise Bakery Co",
    "Vraj Enterprises", "Om Sai Logistics", "Diamond City Exports",
    "Nova Retail Solutions",
]

NARRATIONS = [
    "UPI settlement for order #{oid}",
    "Payout - order {oid} - NEFT",
    "Settlement batch order {oid}",
    "Order {oid} payment credited",
    "Txn {oid} settled via IMPS",
]

def typo(text):
    """Introduce a realistic single-character human-entry typo."""
    if len(text) < 4:
        return text
    i = random.randint(1, len(text) - 2)
    swaps = {"a": "s", "e": "r", "o": "0", "l": "1", "S": "5"}
    c = text[i]
    new_c = swaps.get(c, c)
    return text[:i] + new_c + text[i+1:]

def gen_ref(idx):
    return f"RZP{2026000000 + idx}"

def generate(n=60):
    gateway_rows = []
    ledger_rows = []
    base_date = datetime(2026, 7, 1)

    for i in range(n):
        oid = f"ORD{10000+i}"
        ref = gen_ref(i)
        merchant = random.choice(MERCHANTS)
        amount = round(random.uniform(500, 45000), 2)
        settle_date = base_date + timedelta(days=random.randint(0, 45))
        narration = random.choice(NARRATIONS).format(oid=oid)

        case = random.choices(
            ["clean", "typo", "partial_ref", "amount_delta", "date_drift",
             "orphan_gateway", "orphan_ledger", "low_confidence", "duplicate_ledger"],
            weights=[28, 13, 9, 13, 13, 5, 5, 9, 5], k=1
        )[0]

        gw_ref, gw_amt, gw_date, gw_narr, gw_merchant = ref, amount, settle_date, narration, merchant
        gw_oid = oid  # gateway-side order_ref (join key) — may diverge from ledger's below

        if case == "clean":
            ld_oid, ld_amt, ld_date, ld_narr, ld_merchant = oid, amount, settle_date, narration, merchant

        elif case == "typo":
            # ledger's own order_ref field has a human-entry typo -> exact join key FAILS,
            # fuzzy match must rescue it using merchant/narration/amount/date instead
            ld_oid = typo(oid)
            ld_amt = amount
            ld_date = settle_date
            ld_narr = typo(narration.replace(oid, ld_oid))
            ld_merchant = typo(merchant)

        elif case == "partial_ref":
            # ledger only stored a truncated reference — join key breaks,
            # fuzzy match must rely on merchant + amount + date proximity
            ld_oid = oid[-5:]
            ld_amt, ld_date, ld_merchant = amount, settle_date, merchant
            ld_narr = f"Settled - ref {ld_oid}"

        elif case == "amount_delta":
            ld_oid, ld_date, ld_narr, ld_merchant = oid, settle_date, narration, merchant
            fee = round(random.uniform(2, 15), 2)
            ld_amt = round(amount - fee, 2)  # gateway fee deducted, ledger booked gross

        elif case == "date_drift":
            ld_oid, ld_amt, ld_narr, ld_merchant = oid, amount, narration, merchant
            ld_date = settle_date - timedelta(days=random.randint(1, 3))
            gw_date = settle_date  # gateway settles later than ledger booking

        elif case == "low_confidence":
            # Several weak signals stacked at once — genuinely ambiguous, not
            # cleanly resolvable by any single signal. Should land in the
            # NEEDS_REVIEW band rather than auto-matching or auto-rejecting.
            ld_oid = oid[-5:]                                   # broken reference (as partial_ref)
            ld_merchant = typo(random.choice(MERCHANTS))         # different-ish merchant text
            ld_narr = f"Payment ref {ld_oid}"                    # narration shares little with gateway's
            pct_off = random.uniform(3, 7)                       # moderate amount drift (%), beyond tolerance
            ld_amt = round(amount * (1 - pct_off / 100), 2)
            ld_date = settle_date - timedelta(days=random.randint(2, 3))  # moderate date drift

        elif case == "duplicate_ledger":
            # Same order booked twice internally (data-entry duplicate).
            # Only one can match the single gateway settlement; the other
            # must correctly fall out as an exception, not silently vanish
            # or double-count.
            ld_oid, ld_amt, ld_date, ld_narr, ld_merchant = oid, amount, settle_date, narration, merchant
            gateway_rows.append({
                "settlement_id": gw_ref, "order_ref": gw_oid, "merchant": gw_merchant,
                "amount": gw_amt, "date": gw_date.strftime("%Y-%m-%d"), "narration": gw_narr,
            })
            ledger_rows.append({
                "entry_id": f"LG{5000+i}", "order_ref": ld_oid, "merchant": ld_merchant,
                "amount": ld_amt, "date": ld_date.strftime("%Y-%m-%d"), "narration": ld_narr,
            })
            ledger_rows.append({
                "entry_id": f"LG{5000+i}DUP", "order_ref": ld_oid, "merchant": ld_merchant,
                "amount": ld_amt, "date": ld_date.strftime("%Y-%m-%d"),
                "narration": ld_narr + " (duplicate entry)",
            })
            continue

        elif case == "orphan_gateway":
            # gateway paid out, never appears in ledger at all
            gateway_rows.append({
                "settlement_id": ref, "order_ref": oid, "merchant": merchant,
                "amount": amount, "date": settle_date.strftime("%Y-%m-%d"),
                "narration": narration,
            })
            continue

        elif case == "orphan_ledger":
            ledger_rows.append({
                "entry_id": f"LG{5000+i}", "order_ref": oid, "merchant": merchant,
                "amount": amount, "date": settle_date.strftime("%Y-%m-%d"),
                "narration": narration,
            })
            continue

        gateway_rows.append({
            "settlement_id": gw_ref, "order_ref": gw_oid, "merchant": gw_merchant,
            "amount": gw_amt, "date": gw_date.strftime("%Y-%m-%d"),
            "narration": gw_narr,
        })
        ledger_rows.append({
            "entry_id": f"LG{5000+i}", "order_ref": ld_oid, "merchant": ld_merchant,
            "amount": ld_amt, "date": ld_date.strftime("%Y-%m-%d"),
            "narration": ld_narr,
        })

    return gateway_rows, ledger_rows


def write_csv(rows, path, fieldnames):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    import os
    # Resolve paths relative to this script's own location, not the current
    # working directory — so it works whether you run it from backend/,
    # backend/app/, or anywhere else.
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(here, "..", "data")
    os.makedirs(data_dir, exist_ok=True)

    gw, lg = generate(60)
    write_csv(gw, os.path.join(data_dir, "gateway_settlements.csv"),
               ["settlement_id", "order_ref", "merchant", "amount", "date", "narration"])
    write_csv(lg, os.path.join(data_dir, "internal_ledger.csv"),
               ["entry_id", "order_ref", "merchant", "amount", "date", "narration"])
    print(f"Generated {len(gw)} gateway records, {len(lg)} ledger records")
    print(f"Written to: {os.path.abspath(data_dir)}")
