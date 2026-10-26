"""
services/payments/gateway.py
----------------------------
The only code that talks to Razorpay.

Modes (PAYMENTS_MODE):
- razorpay: real API with RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET.
- mock:     development only (refused in production by config.py). Orders
            and refunds are fake ids; a dev-only endpoint marks an order
            paid so the full flow can be clicked through without money.

Route (split payments to each business's linked account) is OFF unless
ROUTE_ENABLED=true AND the business has a linked_account_id. RR is
waiting on Razorpay to confirm eligibility under the RBI payment
aggregator rules. The transfer fields used below follow Razorpay's
Route docs; check them against the live docs before switching it on.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

import httpx

from app_platform.config import get_settings
from app_platform.errors import AppError
from app_platform.logger import get_logger

log = get_logger("payments.gateway")
API = "https://api.razorpay.com/v1"


class GatewayError(AppError):
    def __init__(self, message: str = "The payment service didn't respond. Please try again."):
        super().__init__(502, "payment_gateway", message)


def _auth() -> tuple[str, str]:
    s = get_settings()
    if not (s.razorpay_key_id and s.razorpay_key_secret):
        raise GatewayError("Payments are not configured yet.")
    return s.razorpay_key_id, s.razorpay_key_secret


def is_mock() -> bool:
    return get_settings().payments_mode == "mock"


def public_key() -> str:
    return "mock" if is_mock() else get_settings().razorpay_key_id


def create_order(amount_paise: int, receipt: str, notes: dict, transfers: list | None = None) -> str:
    if is_mock():
        return f"order_mock_{secrets.token_hex(8)}"
    payload = {"amount": amount_paise, "currency": "INR", "receipt": receipt[:40], "notes": notes}
    if transfers:
        payload["transfers"] = transfers
    try:
        r = httpx.post(f"{API}/orders", json=payload, auth=_auth(), timeout=20)
    except httpx.HTTPError as exc:
        log.error("create_order network error %s", type(exc).__name__)
        raise GatewayError() from exc
    if r.status_code >= 300:
        log.error("create_order failed status=%s", r.status_code)
        raise GatewayError()
    return r.json()["id"]


def fetch_payment(payment_id: str) -> dict:
    if is_mock():
        return {"id": payment_id, "status": "captured"}
    try:
        r = httpx.get(f"{API}/payments/{payment_id}", auth=_auth(), timeout=20)
    except httpx.HTTPError as exc:
        raise GatewayError() from exc
    if r.status_code >= 300:
        raise GatewayError()
    return r.json()


def refund(payment_id: str, amount_paise: int, reverse_transfers: bool = False) -> str:
    if is_mock():
        return f"rfnd_mock_{secrets.token_hex(6)}"
    payload: dict = {"amount": amount_paise, "speed": "normal"}
    if reverse_transfers:
        payload["reverse_all"] = 1
    try:
        r = httpx.post(f"{API}/payments/{payment_id}/refund", json=payload, auth=_auth(), timeout=20)
    except httpx.HTTPError as exc:
        raise GatewayError("The refund couldn't be started. Please try again.") from exc
    if r.status_code >= 300:
        log.error("refund failed payment=%s status=%s", payment_id, r.status_code)
        raise GatewayError("The refund couldn't be started. Please try again.")
    return r.json()["id"]


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Razorpay Checkout returns HMAC_SHA256(order_id|payment_id, key_secret)."""
    secret = get_settings().razorpay_key_secret.encode()
    if not secret:
        return False
    expected = hmac.new(secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = get_settings().razorpay_webhook_secret.encode()
    if not secret:
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def route_transfers(linked_account_id: str, amount_paise: int, hold_until_unix: int) -> list | None:
    s = get_settings()
    if not (s.route_enabled and linked_account_id):
        return None
    return [{
        "account": linked_account_id, "amount": amount_paise, "currency": "INR",
        "on_hold": 1, "on_hold_until": hold_until_unix,
    }]
