"""SQLite storage for InvoicePilot."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("INVOICEPILOT_DB", Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    invoice_number TEXT,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    due_date TEXT,
    issued_date TEXT,
    line_items TEXT NOT NULL DEFAULT '[]',
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    paypal_invoice_id TEXT,
    paypal_approve_url TEXT,
    payer_email TEXT,
    paid_at TEXT,
    created_at TEXT NOT NULL,
    source_text TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def add_event(conn: sqlite3.Connection, invoice_id: int | None, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events (invoice_id, kind, detail, created_at) VALUES (?,?,?,?)",
        (invoice_id, kind, detail, _now()),
    )


def create_invoice(data: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO invoices
               (vendor, invoice_number, amount_cents, currency, due_date, issued_date,
                line_items, notes, status, source_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["vendor"],
                data.get("invoice_number"),
                int(data["amount_cents"]),
                data.get("currency", "USD"),
                data.get("due_date"),
                data.get("issued_date"),
                data.get("line_items", "[]"),
                data.get("notes"),
                data.get("status", "draft"),
                data.get("source_text"),
                _now(),
            ),
        )
        invoice_id = int(cur.lastrowid)
        add_event(conn, invoice_id, "created", f"{data['vendor']} {data['amount_cents'] / 100:.2f}")
        return invoice_id


def get_invoice(invoice_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()


def list_invoices() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM invoices ORDER BY id DESC").fetchall()


def list_events(invoice_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE invoice_id = ? ORDER BY id DESC", (invoice_id,)
        ).fetchall()


def set_paypal(invoice_id: int, invoice_id_pp: str, approve_url: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE invoices SET paypal_invoice_id = ?, paypal_approve_url = ?, status = 'sent' WHERE id = ?",
            (invoice_id_pp, approve_url, invoice_id),
        )
        add_event(conn, invoice_id, "paypal_invoice_created", invoice_id_pp)


def mark_paid(invoice_id: int, detail: str = "paypal webhook") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?", (_now(), invoice_id)
        )
        add_event(conn, invoice_id, "paid", detail)


def stats() -> dict:
    today = date.today().isoformat()
    with connect() as conn:
        rows = conn.execute("SELECT status, amount_cents, due_date FROM invoices").fetchall()
    outstanding = sum(r["amount_cents"] for r in rows if r["status"] != "paid")
    overdue = sum(
        r["amount_cents"]
        for r in rows
        if r["status"] != "paid" and r["due_date"] and r["due_date"] < today
    )
    paid = sum(r["amount_cents"] for r in rows if r["status"] == "paid")
    return {
        "count": len(rows),
        "outstanding": outstanding,
        "overdue": overdue,
        "paid": paid,
        "overdue_count": sum(
            1 for r in rows if r["status"] != "paid" and r["due_date"] and r["due_date"] < today
        ),
    }
