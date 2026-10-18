# InvoicePilot

An AI accounts-receivable agent for small businesses. Paste an invoice, let AI
read it, collect through PayPal, and get a drafted reminder when it goes late.

Built for the PayPal AI Hackathon (2026).

## Why

Small teams lose hours every week to chasing invoices: retyping totals into a
payment tool, noticing a payment is late only when the bank balance looks wrong,
and writing the same awkward reminder email again. InvoicePilot turns that into
one paste and one click.

## What it does

1. **Read** — paste invoice text or upload a PDF. An LLM extracts vendor,
   invoice number, total, currency and due date. Deterministic parsing is the
   fallback, so the app works with no model configured.
2. **Collect** — creates a PayPal invoice (Invoicing API v2) or a one-off Order
   (Orders API v2) and surfaces the payer link.
3. **Reconcile** — a PayPal webhook (`INVOICING.INVOICE.PAID`,
   `PAYMENT.CAPTURE.COMPLETED`) marks the invoice paid and writes an audit trail.
4. **Chase** — drafts a friendly / firm / final reminder in one click, grounded
   in the invoice's own numbers.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8099
```

Open http://localhost:8099.

### PayPal sandbox

Create an app at https://developer.paypal.com/ and set:

```bash
export PAYPAL_CLIENT_ID=...
export PAYPAL_CLIENT_SECRET=...
export PAYPAL_ENV=sandbox
```

Without credentials the app runs in **mock mode**: the payment link points to a
local checkout that marks the invoice paid, so the whole flow is demonstrable
offline.

### AI model

Any OpenAI-compatible chat-completions endpoint:

```bash
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_API_KEY=...
export INVOICEPILOT_MODEL=gpt-4o-mini
```

With none set, extraction and reminders use built-in heuristics.

## Stack

FastAPI · Jinja2 · SQLite · httpx · PayPal REST v2

## License

MIT — see `LICENSE`.
