"""PayPal REST integration (sandbox by default) with a working mock mode.

Real mode needs PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET. Without them the app
still runs end-to-end: it returns a local /mock/pay/<id> approval link so the
demo can show the full collect -> paid flow without network access.
"""
from __future__ import annotations

import base64
import os
import time
import uuid

import httpx

SANDBOX = os.environ.get("PAYPAL_ENV", "sandbox") != "live"
BASE = "https://api-m.paypal.com" if not SANDBOX else "https://api-m.sandbox.paypal.com"
CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
CURRENCY = os.environ.get("PAYPAL_CURRENCY", "USD")

_token: dict = {"value": None, "expires": 0.0}


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def _access_token() -> str:
    now = time.time()
    if _token["value"] and now < _token["expires"] - 30:
        return _token["value"]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = httpx.post(
        f"{BASE}/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    _token["value"] = payload["access_token"]
    _token["expires"] = now + int(payload.get("expires_in", 3000))
    return _token["value"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": str(uuid.uuid4()),
    }


def create_invoice(invoice: dict) -> tuple[str, str]:
    """Create a PayPal invoice and return (paypal_invoice_id, approval_url)."""
    if not configured():
        fake_id = f"MOCK-INV-{invoice['id']}"
        return fake_id, f"/mock/pay/{invoice['id']}"

    amount = f"{invoice['amount_cents'] / 100:.2f}"
    body = {
        "detail": {
            "invoice_number": invoice.get("invoice_number") or f"INV-{invoice['id']:04d}",
            "currency_code": invoice.get("currency") or CURRENCY,
            "note": invoice.get("notes") or "InvoicePilot collection",
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": invoice.get("due_date") or "2026-12-31"},
        },
        "invoicer": {"name": {"given_name": "InvoicePilot"}, "email_address": os.environ.get("PAYPAL_INVOICER_EMAIL", "merchant@example.com")},
        "primary_recipients": [
            {
                "billing_info": {
                    "name": {"given_name": invoice.get("vendor") or "Client"},
                    "email_address": invoice.get("payer_email") or os.environ.get("PAYPAL_PAYER_EMAIL", "client@example.com"),
                }
            }
        ],
        "items": [
            {
                "name": "Invoice total",
                "quantity": "1",
                "unit_amount": {"currency_code": invoice.get("currency") or CURRENCY, "value": amount},
            }
        ],
    }
    r = httpx.post(f"{BASE}/v2/invoicing/invoices", headers=_headers(), json=body, timeout=25)
    r.raise_for_status()
    invoice_id = r.json().get("id") or r.headers.get("PayPal-Request-Id", "")
    # Sending the invoice returns the payer-facing link.
    s = httpx.post(
        f"{BASE}/v2/invoicing/invoices/{invoice_id}/send",
        headers=_headers(),
        json={"send_to_invoicer": False},
        timeout=25,
    )
    approve = ""
    if s.status_code < 300:
        approve = s.json().get("href", "")
    return invoice_id, approve


def get_invoice(paypal_invoice_id: str) -> dict | None:
    if not configured():
        return None
    r = httpx.get(f"{BASE}/v2/invoicing/invoices/{paypal_invoice_id}", headers=_headers(), timeout=20)
    if r.status_code >= 300:
        return None
    return r.json()


def create_order(invoice: dict) -> tuple[str, str]:
    """Create a one-off order and return (order_id, approve_url)."""
    if not configured():
        return f"MOCK-ORDER-{invoice['id']}", f"/mock/pay/{invoice['id']}"
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": str(invoice["id"]),
                "description": f"Invoice {invoice.get('invoice_number') or invoice['id']}",
                "amount": {
                    "currency_code": invoice.get("currency") or CURRENCY,
                    "value": f"{invoice['amount_cents'] / 100:.2f}",
                },
            }
        ],
        "application_context": {"brand_name": "InvoicePilot", "user_action": "PAY_NOW"},
    }
    r = httpx.post(f"{BASE}/v2/checkout/orders", headers=_headers(), json=body, timeout=25)
    r.raise_for_status()
    data = r.json()
    approve = next((l["href"] for l in data.get("links", []) if l["rel"] in ("approve", "payer-action")), "")
    return data["id"], approve


def capture_order(order_id: str) -> bool:
    if not configured():
        return True
    r = httpx.post(f"{BASE}/v2/checkout/orders/{order_id}/capture", headers=_headers(), json={}, timeout=25)
    return r.status_code < 300
