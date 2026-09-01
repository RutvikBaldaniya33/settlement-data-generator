"""
SettleSense matching configuration.

Every threshold and weight the reconciliation engine uses lives here, with
the reasoning next to it. Change these to retune behavior — nothing else
in matching.py should hard-code a number.
"""

# ---- Signal weights (must sum to 1.0) -------------------------------------
# How much each signal contributes to the final confidence score.
# Reference is weighted highest because a matching order reference is the
# strongest single signal — but it is NOT sufficient alone (see the amount
# mismatch example in the README): a wrong amount still drags the weighted
# score down below the auto-match bar even with a perfect reference match.
SIGNAL_WEIGHTS = {
    "reference": 0.30,
    "merchant": 0.15,
    "narration": 0.15,
    "amount": 0.25,
    "date": 0.15,
}
assert abs(sum(SIGNAL_WEIGHTS.values()) - 1.0) < 1e-6, "weights must sum to 1.0"

# ---- Amount matching --------------------------------------------------
# A record pair scores full marks on the amount signal if the difference is
# within this absolute tolerance (covers typical gateway fee deductions),
# OR within this percentage of the larger amount (covers proportional fees
# on larger transactions). Score decays linearly to 0 by 5x the tolerance.
AMOUNT_TOLERANCE_ABS_INR = 20.0
AMOUNT_TOLERANCE_PCT = 0.5  # percent
AMOUNT_SCORE_DECAY_MULTIPLE = 5.0

# ---- Date matching ------------------------------------------------------
# Full marks if settlement and ledger dates are within this many days
# (settlement commonly lags ledger booking by a day or two). Score decays
# linearly to 0 at 3x this window.
DATE_TOLERANCE_DAYS = 4
DATE_SCORE_DECAY_MULTIPLE = 3.0

# ---- Decision thresholds -------------------------------------------------
# Applied to the final weighted confidence score (0.0 - 1.0).
#   >= AUTO_MATCH_THRESHOLD        -> MATCHED (system auto-confirms)
#   >= NEEDS_REVIEW_THRESHOLD      -> NEEDS_REVIEW (flagged for a human)
#   <  NEEDS_REVIEW_THRESHOLD      -> EXCEPTION (no credible candidate)
AUTO_MATCH_THRESHOLD = 0.85
NEEDS_REVIEW_THRESHOLD = 0.55

# A candidate pair below this score is not considered a candidate at all —
# it's excluded from the assignment problem entirely rather than being
# forced into a 1:1 match just because nothing better was available.
CANDIDATE_FLOOR = 0.35

# Status labels — used consistently across backend and frontend.
STATUS_MATCHED = "MATCHED"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_EXCEPTION = "EXCEPTION"
