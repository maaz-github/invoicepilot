"""InvoicePilot — an AI accounts-receivable agent for small businesses.

Flow: paste an invoice -> AI extracts the fields -> create a PayPal invoice or
order -> share the payer link -> PayPal webhook (or the mock checkout) marks it
paid -> AI drafts the reminder when it is late.
"""
from __future__ import annotations

import io
import json
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, extract, paypal_client

BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(title="InvoicePilot")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

db.init_db()


def money(cents: int, currency: str = "USD") -> str:
    return f"{currency} {cents / 100:,.2f}"


templates.env.filters["money"] = money


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "invoices": db.list_invoices(), "stats": db.stats(),
         "paypal_live": paypal_client.configured(), "today": date.today().isoformat()},
    )


@app.get("/invoices/new", response_class=HTMLResponse)
async def new_invoice(request: Request):
    return templates.TemplateResponse(request, "new.html", {"request": request})


def _text_from_upload(filename: str, raw: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:  # pragma: no cover - optional dependency
            raise HTTPException(400, f"PDF read failed: {exc}") from exc
    return raw.decode("utf-8", errors="replace")


@app.post("/invoices/extract", response_class=HTMLResponse)
async def extract_preview(request: Request, text: str = Form(""), file: UploadFile | None = None):
    source = text
    if file is not None and file.filename:
        source = _text_from_upload(file.filename, await file.read())
    if not source.strip():
        raise HTTPException(400, "Paste invoice text or upload a file first")
    fields, engine = extract.extract_invoice(source)
    return templates.TemplateResponse(
        request,
        "preview.html",
        {"request": request, "f": fields, "engine": engine, "source": source[:20000]},
    )


@app.post("/invoices")
async def create_invoice(
    vendor: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("USD"),
    due_date: str = Form(""),
    invoice_number: str = Form(""),
    notes: str = Form(""),
    source_text: str = Form(""),
):
    try:
        cents = int(round(float(amount.replace(",", "")) * 100))
    except ValueError as exc:
        raise HTTPException(400, "Amount must be a number") from exc
    invoice_id = db.create_invoice(
        {
            "vendor": vendor.strip(),
            "invoice_number": invoice_number.strip() or None,
            "amount_cents": cents,
            "currency": currency.strip().upper() or "USD",
            "due_date": due_date.strip() or None,
            "notes": notes.strip() or None,
            "source_text": source_text[:20000],
            "status": "draft",
        }
    )
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail(request: Request, invoice_id: int):
    row = db.get_invoice(invoice_id)
    if not row:
        raise HTTPException(404, "Invoice not found")
    return templates.TemplateResponse(
        request,
        "invoice.html",
        {"request": request, "inv": row, "events": db.list_events(invoice_id),
         "paypal_live": paypal_client.configured(), "today": date.today().isoformat()},
    )


@app.post("/invoices/{invoice_id}/paypal")
async def create_paypal(invoice_id: int):
    row = db.get_invoice(invoice_id)
    if not row:
        raise HTTPException(404, "Invoice not found")
    inv = dict(row)
    pp_id, approve = paypal_client.create_invoice(inv)
    if not approve:
        order_id, approve = paypal_client.create_order(inv)
        pp_id = pp_id or order_id
    db.set_paypal(invoice_id, pp_id, approve)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/invoices/{invoice_id}/remind", response_class=HTMLResponse)
async def remind(request: Request, invoice_id: int, tone: str = Form("friendly")):
    row = db.get_invoice(invoice_id)
    if not row:
        raise HTTPException(404, "Invoice not found")
    message, engine = extract.draft_reminder(dict(row), tone)
    return templates.TemplateResponse(
        request,
        "reminder.html",
        {"request": request, "inv": row, "message": message, "engine": engine},
    )


@app.get("/mock/pay/{invoice_id}", response_class=HTMLResponse)
async def mock_pay(request: Request, invoice_id: int):
    row = db.get_invoice(invoice_id)
    if not row:
        raise HTTPException(404, "Invoice not found")
    return templates.TemplateResponse(request, "mock_pay.html", {"request": request, "inv": row})


@app.post("/mock/pay/{invoice_id}")
async def mock_capture(invoice_id: int):
    db.mark_paid(invoice_id, "mock checkout (no PayPal credentials configured)")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.post("/webhook/paypal")
async def paypal_webhook(request: Request):
    payload = await request.json()
    event = payload.get("event_type", "")
    resource = payload.get("resource", {}) or {}
    ref = (
        resource.get("id")
        or (resource.get("purchase_units") or [{}])[0].get("reference_id")
        or resource.get("invoice_id")
    )
    if event in {"INVOICING.INVOICE.PAID", "CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"}:
        for row in db.list_invoices():
            if ref and (row["paypal_invoice_id"] == ref or str(row["id"]) == str(ref)):
                db.mark_paid(row["id"], f"webhook {event}")
                break
    return JSONResponse({"ok": True})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "paypal": paypal_client.configured()}
