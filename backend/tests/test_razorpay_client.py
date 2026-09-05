"""
Tests for app/razorpay_client.py.

No real network calls are made — requests.get is mocked throughout, so
these tests run offline and never touch the real Razorpay API, live or
test mode. Environment variables are set/cleared per-test via monkeypatch
so tests never depend on (or leak into) the developer's real .env.

Run from backend/: python -m pytest tests/test_razorpay_client.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import razorpay_client as rzp


TEST_KEY_ID = "rzp_test_abcdefgh1234"
TEST_KEY_SECRET = "supersecretvalue"


def _clear_env(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)


# --------------------------------------------------------------------------
# Credential presence / shape validation (no network call should happen)
# --------------------------------------------------------------------------

def test_missing_credentials_raise_config_error(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(rzp.RazorpayConfigError):
        rzp.test_connection()


def test_missing_secret_only_raises_config_error(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    with pytest.raises(rzp.RazorpayConfigError):
        rzp.test_connection()


def test_live_mode_key_is_refused(monkeypatch):
    """Hard requirement: this integration must refuse to run against a live key."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_realmoney123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    with pytest.raises(rzp.RazorpayConfigError, match="LIVE mode"):
        rzp.test_connection()


def test_malformed_key_id_is_rejected(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "not_a_razorpay_key")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)
    with pytest.raises(rzp.RazorpayConfigError):
        rzp.test_connection()


@patch("razorpay_client.requests.get")
def test_config_errors_never_make_a_network_call(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(rzp.RazorpayConfigError):
        rzp.test_connection()
    mock_get.assert_not_called()


# --------------------------------------------------------------------------
# Successful connection
# --------------------------------------------------------------------------

@patch("razorpay_client.requests.get")
def test_successful_connection_returns_masked_key_no_secret(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    result = rzp.test_connection()

    assert result["connected"] is True
    assert result["mode"] == "test"
    assert TEST_KEY_SECRET not in str(result)
    assert result["key_id"] != TEST_KEY_ID  # masked, not echoed in full


@patch("razorpay_client.requests.get")
def test_successful_connection_uses_basic_auth_with_env_credentials(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    rzp.test_connection()

    _, kwargs = mock_get.call_args
    assert kwargs["auth"] == (TEST_KEY_ID, TEST_KEY_SECRET)
    assert kwargs["timeout"] == rzp._REQUEST_TIMEOUT_SECONDS
    assert "razorpay.com" in mock_get.call_args[0][0]


# --------------------------------------------------------------------------
# Authentication failure (wrong/revoked credentials)
# --------------------------------------------------------------------------

@patch("razorpay_client.requests.get")
def test_401_raises_auth_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "wrongsecret")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_get.return_value = mock_resp

    with pytest.raises(rzp.RazorpayAuthError):
        rzp.test_connection()


@patch("razorpay_client.requests.get")
def test_403_raises_auth_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_get.return_value = mock_resp

    with pytest.raises(rzp.RazorpayAuthError):
        rzp.test_connection()


# --------------------------------------------------------------------------
# Other API errors / network failures
# --------------------------------------------------------------------------

@patch("razorpay_client.requests.get")
def test_5xx_raises_api_error_with_razorpay_message(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {"error": {"description": "Internal server error"}}
    mock_get.return_value = mock_resp

    with pytest.raises(rzp.RazorpayAPIError, match="Internal server error"):
        rzp.test_connection()


@patch("razorpay_client.requests.get")
def test_non_json_error_body_does_not_crash(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.side_effect = ValueError("not json")
    mock_get.return_value = mock_resp

    with pytest.raises(rzp.RazorpayAPIError):
        rzp.test_connection()


@patch("razorpay_client.requests.get")
def test_timeout_raises_api_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    import requests
    mock_get.side_effect = requests.exceptions.Timeout()

    with pytest.raises(rzp.RazorpayAPIError, match="Timed out"):
        rzp.test_connection()


@patch("razorpay_client.requests.get")
def test_connection_error_raises_api_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    import requests
    mock_get.side_effect = requests.exceptions.ConnectionError()

    with pytest.raises(rzp.RazorpayAPIError, match="Could not reach"):
        rzp.test_connection()


# --------------------------------------------------------------------------
# Isolation from the reconciliation engine
# --------------------------------------------------------------------------

def test_matching_module_does_not_import_razorpay_client():
    """Hard requirement: Razorpay-specific code (imports/calls) must stay
    out of matching.py. Prose comments mentioning Razorpay as a future
    extension point (e.g. in the reconcile() docstring) are fine and
    expected — this checks for actual coupling, not the word itself."""
    import matching
    assert not hasattr(matching, "razorpay_client")
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "matching.py")) as f:
        src = f.read()
    assert "import razorpay" not in src
    assert "razorpay_client." not in src


def test_normalize_module_does_not_import_razorpay_client():
    """Same check for normalize.py: it may document Razorpay as a future
    adapter target, but must not actually import or call razorpay_client."""
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "normalize.py")) as f:
        src = f.read()
    assert "import razorpay" not in src
    assert "razorpay_client." not in src


# --------------------------------------------------------------------------
# fetch_payments() — used by the /api/razorpay/reconcile integration
# --------------------------------------------------------------------------

def test_fetch_payments_missing_credentials_raise_config_error(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(rzp.RazorpayConfigError):
        rzp.fetch_payments(count=5)


@patch("razorpay_client.requests.get")
def test_fetch_payments_config_errors_never_make_a_network_call(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(rzp.RazorpayConfigError):
        rzp.fetch_payments(count=5)
    mock_get.assert_not_called()


@patch("razorpay_client.requests.get")
def test_fetch_payments_returns_raw_items_list(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    raw_items = [
        {"id": "pay_1", "order_id": "ORD1", "amount": "150050", "created_at": 1751328000},
        {"id": "pay_2", "order_id": "ORD2", "amount": "200000", "created_at": 1751328000},
    ]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"items": raw_items}
    mock_get.return_value = mock_resp

    result = rzp.fetch_payments(count=2)

    assert result == raw_items
    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {"count": 2}
    assert kwargs["auth"] == (TEST_KEY_ID, TEST_KEY_SECRET)
    assert TEST_KEY_SECRET not in str(result)


@patch("razorpay_client.requests.get")
def test_fetch_payments_401_raises_auth_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "wrongsecret")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_get.return_value = mock_resp

    with pytest.raises(rzp.RazorpayAuthError):
        rzp.fetch_payments(count=5)


@patch("razorpay_client.requests.get")
def test_fetch_payments_non_json_success_body_raises_api_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("not json")
    mock_get.return_value = mock_resp

    with pytest.raises(rzp.RazorpayAPIError):
        rzp.fetch_payments(count=5)


@patch("razorpay_client.requests.get")
def test_fetch_payments_timeout_raises_api_error(mock_get, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", TEST_KEY_SECRET)

    import requests
    mock_get.side_effect = requests.exceptions.Timeout()

    with pytest.raises(rzp.RazorpayAPIError, match="Timed out"):
        rzp.fetch_payments(count=5)