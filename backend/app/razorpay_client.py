"""
SettleSense — Razorpay TEST MODE integration (Step 3: connection only).

This is the ONLY module in the backend that talks to Razorpay. Nothing in
matching.py, normalize.py, or main.py's core reconciliation flow knows
Razorpay exists — this module exists precisely so that stays true.

What this module does:
  - reads RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from environment variables
    (never hard-coded, never logged, never returned to any caller)
  - refuses to proceed if the configured key looks like a LIVE mode key
    (rzp_live_...) — this integration is TEST MODE ONLY, by design
  - exposes test_connection(), a single lightweight authenticated call to
    Razorpay's API that proves the credentials actually work

What this module deliberately does NOT do yet (out of scope for Step 3):
  - fetch or reconcile any real settlement/payment data
  - register or handle webhooks
  - get imported by matching.py, normalize.py, or any reconciliation code
    (a future Razorpay `normalize.py` adapter would call INTO this module
    to fetch raw rows, never the other way around)
"""
import os
import requests

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
TEST_KEY_PREFIX = "rzp_test_"
LIVE_KEY_PREFIX = "rzp_live_"
_REQUEST_TIMEOUT_SECONDS = 10


class RazorpayConfigError(Exception):
    """Credentials are missing, or don't look like a TEST mode key."""


class RazorpayAuthError(Exception):
    """A request was sent but Razorpay rejected the credentials (401/403)."""


class RazorpayAPIError(Exception):
    """Razorpay was reached but returned an unexpected error, or the
    request could not be completed at all (network/timeout/DNS)."""


def _get_credentials():
    """Reads credentials from the environment ONLY. Never accepts a key as
    a function argument, a query param, or anything else that could leak
    into logs, error messages, or the frontend."""
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")

    if not key_id or not key_secret:
        raise RazorpayConfigError(
            "RAZORPAY_KEY_ID and/or RAZORPAY_KEY_SECRET are not set in the "
            "environment. See README for how to set them locally — they "
            "must never be hard-coded in source."
        )

    if key_id.startswith(LIVE_KEY_PREFIX):
        raise RazorpayConfigError(
            f"RAZORPAY_KEY_ID looks like a LIVE mode key (starts with "
            f"'{LIVE_KEY_PREFIX}'). This integration only supports TEST "
            f"mode keys (starting with '{TEST_KEY_PREFIX}') — refusing to "
            f"connect with a live key."
        )

    if not key_id.startswith(TEST_KEY_PREFIX):
        raise RazorpayConfigError(
            f"RAZORPAY_KEY_ID does not look like a Razorpay TEST mode key "
            f"(expected a '{TEST_KEY_PREFIX}' prefix). Generate test API "
            f"keys from the Razorpay Dashboard with 'Test Mode' toggled on."
        )

    return key_id, key_secret


def _mask(key_id: str) -> str:
    """key_id is a public identifier, not a secret — but we still avoid
    echoing it back in full, out of caution. The secret is NEVER passed to
    this function or included in any response, anywhere in this module."""
    if len(key_id) <= 12:
        return "***"
    return f"{key_id[:8]}...{key_id[-4:]}"


def test_connection() -> dict:
    """One lightweight authenticated GET against Razorpay TEST mode, just to
    confirm the configured credentials work. Does not fetch or return any
    payment/settlement data beyond the fact that the call succeeded.

    Raises:
        RazorpayConfigError  - credentials missing or not a test-mode key
        RazorpayAuthError    - credentials rejected by Razorpay
        RazorpayAPIError     - network failure or an unexpected API error
    """
    key_id, key_secret = _get_credentials()

    try:
        resp = requests.get(
            f"{RAZORPAY_API_BASE}/payments",
            params={"count": 1},
            auth=(key_id, key_secret),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        raise RazorpayAPIError("Timed out connecting to Razorpay. Check your network and try again.")
    except requests.exceptions.ConnectionError:
        raise RazorpayAPIError("Could not reach Razorpay's API (connection error). Check your network.")
    except requests.exceptions.RequestException as e:
        raise RazorpayAPIError(f"Unexpected error contacting Razorpay: {e}")

    if resp.status_code == 200:
        return {
            "connected": True,
            "mode": "test",
            "key_id": _mask(key_id),
        }

    if resp.status_code in (401, 403):
        raise RazorpayAuthError(
            "Razorpay rejected the configured credentials (authentication "
            "failed). Double-check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET "
            "in your environment."
        )

    detail = None
    try:
        detail = resp.json().get("error", {}).get("description")
    except ValueError:
        pass
    raise RazorpayAPIError(
        f"Razorpay API returned HTTP {resp.status_code}" + (f": {detail}" if detail else ".")
    )