"""AI extraction + reminder drafting.

Uses any OpenAI-compatible chat-completions endpoint when configured
(OPENAI_BASE_URL / OPENAI_API_KEY / INVOICEPILOT_MODEL). Falls back to
deterministic heuristics so the demo always works, even offline.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime

import httpx

BASE_URL = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("INVOICEPILOT_MODEL", "gpt-4o-mini")
TIMEOUT = float(os.environ.get("INVOICEPILOT_LLM_TIMEOUT", "25"))

EXTRACT_PROMPT = """You extract accounts-payable data from invoice text.
Return ONLY a JSON object with these keys:
  vendor (string), invoice_number (string or null),
  amount_cents (integer, total in minor units),
  currency (3-letter code, default USD),
  due_date (YYYY-MM-DD or null), issued_date (YYYY-MM-DD or null),
  line_items (array of {{"description": string, "amount_cents": integer}}),
  notes (short string or null)
Do not invent values that are not in the text. If the total is ambiguous,
use the largest total-like amount. Today is {today}."""


def _post_chat(messages: list[dict], temperature: float = 0.0) -> str | None:
    if not (BASE_URL and API_KEY):
        return None
    try:
        r = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": messages, "temperature": temperature},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


AMOUNT_RE = re.compile(r"(?:USD|US\$|\$|RM|MYR|EUR|€|GBP|£)\s?([0-9][0-9,]*\.?[0-9]{0,2})", re.I)
BARE_AMOUNT_RE = re.compile(r"\b([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\b")
DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), ("y", "m", "d")),
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), ("d", "m", "y")),
]
MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"], start=1)
}


def _parse_date(text: str) -> str | None:
    for rx, order in DATE_PATTERNS:
        m = rx.search(text)
        if m:
            parts = dict(zip(order, m.groups()))
            try:
                return date(int(parts["y"]), int(parts["m"]), int(parts["d"])).isoformat()
            except ValueError:
                continue
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", text)
    if m:
        mon = None
        probe = m.group(2).lower()[:3]
        for name, num in MONTHS.items():
            if name.startswith(probe):
                mon = num
                break
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1))).isoformat()
            except ValueError:
                pass
    return None


def _to_cents(raw: str) -> int:
    return int(round(float(raw.replace(",", "")) * 100))


def heuristic_extract(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    vendor = lines[0][:120] if lines else "Unknown vendor"

    amounts = [_to_cents(m.group(1)) for m in AMOUNT_RE.finditer(text)]
    if not amounts:
        amounts = [_to_cents(m.group(1)) for m in BARE_AMOUNT_RE.finditer(text)]
    total = max(amounts) if amounts else 0

    inv_no = None
    m = re.search(r"(?:invoice|inv)[\s#:.-]*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", text, re.I)
    if m:
        inv_no = m.group(1)

    due = None
    m = re.search(r"due[^\n]{0,40}", text, re.I)
    if m:
        due = _parse_date(m.group(0))
    due = due or _parse_date(text)

    currency = "USD"
    if re.search(r"\bRM\b|MYR", text):
        currency = "MYR"
    elif "€" in text or "EUR" in text.upper():
        currency = "EUR"
    elif "£" in text or "GBP" in text.upper():
        currency = "GBP"

    return {
        "vendor": vendor,
        "invoice_number": inv_no,
        "amount_cents": total,
        "currency": currency,
        "due_date": due,
        "issued_date": None,
        "line_items": json.dumps([]),
        "notes": None,
    }


def _coerce(payload: dict, text: str) -> dict:
    base = heuristic_extract(text)
    out = dict(base)
    for key in ("vendor", "invoice_number", "currency", "due_date", "issued_date", "notes"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    amount = payload.get("amount_cents")
    if isinstance(amount, (int, float)) and amount > 0:
        out["amount_cents"] = int(amount)
    items = payload.get("line_items")
    if isinstance(items, list):
        out["line_items"] = json.dumps(items)
    return out


def extract_invoice(text: str) -> tuple[dict, str]:
    """Return (fields, engine) where engine is 'llm' or 'heuristic'."""
    raw = _post_chat(
        [
            {"role": "system", "content": EXTRACT_PROMPT.format(today=date.today().isoformat())},
            {"role": "user", "content": text[:12000]},
        ]
    )
    if raw:
        try:
            start, end = raw.find("{"), raw.rfind("}")
            payload = json.loads(raw[start : end + 1])
            return _coerce(payload, text), "llm"
        except Exception:
            pass
    return heuristic_extract(text), "heuristic"


def draft_reminder(invoice: dict, tone: str = "friendly") -> tuple[str, str]:
    """Return (message, engine)."""
    due = invoice.get("due_date") or "the due date"
    amount = invoice["amount_cents"] / 100
    prompt = (
        f"Write a {tone} payment reminder email from a small business to a client. "
        f"Invoice {invoice.get('invoice_number') or invoice['id']} for "
        f"{invoice['currency']} {amount:,.2f} was due {due}. "
        "Keep it under 90 words, no placeholders like [Name], sign as 'Accounts Team'."
    )
    raw = _post_chat([{"role": "user", "content": prompt}], temperature=0.4)
    if raw:
        return raw.strip(), "llm"
    return (
        f"Hi,\n\nA quick reminder that invoice "
        f"{invoice.get('invoice_number') or invoice['id']} for "
        f"{invoice['currency']} {amount:,.2f} was due on {due}. "
        "You can settle it with the PayPal link on the invoice page.\n\n"
        "If payment is already on its way, please ignore this note.\n\n"
        "Thank you,\nAccounts Team",
        "heuristic",
    )
