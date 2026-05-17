"""
Invoicing module.

One-stop API for creating, listing, and managing invoices issued by PSS.
Invoice numbers follow PSS-YYYY-NNNN, sequential per year. Totals are
recomputed on every save from the line_items JSON so the persisted
subtotal/tax/total can never drift from the items shown on the printed
invoice.

Status machine:
    draft   -> sent | void
    sent    -> paid | partial | overdue | void
    partial -> paid | overdue | void          (amount_paid > 0 but < total)
    overdue -> paid | partial | void
    paid    -> void                            (refund / cancellation only)
    void    -> (terminal)

`overdue` is a derived state: a row is overdue if status in (sent, partial)
AND due_at < today. The list view tags this on read; we also offer a manual
`mark_overdue` for the rare case where you want it pinned.

PDF rendering uses [templates/invoice.html](templates/invoice.html) so the
look and feel matches the violation letter (Inter, orange accent, etc).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

from db import connect

PROJECT_ROOT = Path(__file__).resolve().parent


# ---------- Line item helpers ----------

@dataclass
class LineItem:
    description: str
    quantity: float
    unit_price: float

    @property
    def amount(self) -> float:
        return round(self.quantity * self.unit_price, 2)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "quantity":    self.quantity,
            "unit_price":  self.unit_price,
            "amount":      self.amount,
        }


def coerce_line_items(items: list[dict] | str | None) -> list[dict]:
    """Accept a list of dicts or a JSON string; return list of dicts with `amount` filled."""
    if items is None or items == "":
        return []
    if isinstance(items, str):
        items = json.loads(items)
    out: list[dict] = []
    for it in items:
        desc = str(it.get("description") or "").strip()
        qty  = float(it.get("quantity") or 0)
        unit = float(it.get("unit_price") or 0)
        if not desc and not qty and not unit:
            continue
        out.append({
            "description": desc,
            "quantity":    qty,
            "unit_price":  round(unit, 2),
            "amount":      round(qty * unit, 2),
        })
    return out


def compute_totals(items: list[dict], tax_rate: float) -> tuple[float, float, float]:
    """Return (subtotal, tax_amount, total). All values rounded to cents."""
    subtotal = round(sum(it["amount"] for it in items), 2)
    tax_amount = round(subtotal * (tax_rate or 0), 2)
    total = round(subtotal + tax_amount, 2)
    return subtotal, tax_amount, total


# ---------- Invoice numbering ----------

def next_invoice_number(conn, year: int | None = None) -> str:
    """Compute PSS-YYYY-NNNN. NNNN is the next per-year sequence."""
    year = year or date.today().year
    prefix = f"PSS-{year}-"
    row = conn.execute(
        "SELECT invoice_number FROM invoices WHERE invoice_number LIKE ? "
        "ORDER BY invoice_number DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = 1
    if row:
        last = row[0]
        try:
            seq = int(last.split("-")[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f"{prefix}{seq:04d}"


# ---------- CRUD ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_invoice(
    *,
    client_name: str,
    line_items: list[dict],
    client_address: str | None = None,
    client_city: str | None = None,
    client_state: str | None = None,
    client_zip: str | None = None,
    client_email: str | None = None,
    client_phone: str | None = None,
    property_address: str | None = None,
    property_city: str | None = None,
    property_state: str | None = None,
    property_zip: str | None = None,
    source: str | None = None,
    case_number: str | None = None,
    contract_event_id: int | None = None,
    contract_id: int | None = None,
    tax_rate: float = 0.0,
    due_at: str | None = None,
    terms: str | None = "Due on receipt",
    notes: str | None = None,
) -> dict:
    """Create a draft invoice. Returns the saved row as a dict."""
    if not client_name or not client_name.strip():
        raise ValueError("client_name is required")
    items = coerce_line_items(line_items)
    if not items:
        raise ValueError("at least one line item is required")
    subtotal, tax_amount, total = compute_totals(items, tax_rate)
    now = _now()

    with connect() as conn:
        number = next_invoice_number(conn)
        cur = conn.execute(
            """
            INSERT INTO invoices (
                invoice_number, source, case_number, contract_event_id, contract_id,
                client_name, client_address, client_city, client_state, client_zip,
                client_email, client_phone,
                property_address, property_city, property_state, property_zip,
                line_items, subtotal, tax_rate, tax_amount, total, amount_paid,
                status, due_at, terms, notes,
                created_at, updated_at
            ) VALUES (
                :invoice_number, :source, :case_number, :contract_event_id, :contract_id,
                :client_name, :client_address, :client_city, :client_state, :client_zip,
                :client_email, :client_phone,
                :property_address, :property_city, :property_state, :property_zip,
                :line_items, :subtotal, :tax_rate, :tax_amount, :total, 0,
                'draft', :due_at, :terms, :notes,
                :created_at, :updated_at
            )
            """,
            {
                "invoice_number":    number,
                "source":            source,
                "case_number":       case_number,
                "contract_event_id": contract_event_id,
                "contract_id":       contract_id,
                "client_name":       client_name.strip(),
                "client_address":    client_address,
                "client_city":       client_city,
                "client_state":      client_state,
                "client_zip":        client_zip,
                "client_email":      client_email,
                "client_phone":      client_phone,
                "property_address":  property_address,
                "property_city":     property_city,
                "property_state":    property_state,
                "property_zip":      property_zip,
                "line_items":        json.dumps(items),
                "subtotal":          subtotal,
                "tax_rate":          tax_rate or 0.0,
                "tax_amount":        tax_amount,
                "total":             total,
                "due_at":            due_at,
                "terms":             terms,
                "notes":             notes,
                "created_at":        now,
                "updated_at":        now,
            },
        )
        inv_id = cur.lastrowid
    return get_invoice(inv_id)


def update_invoice(invoice_id: int, **fields) -> dict:
    """
    Update an editable invoice. Only allowed while status='draft'. Recomputes
    totals if line_items or tax_rate change.
    """
    inv = get_invoice(invoice_id)
    if inv["status"] != "draft":
        raise ValueError(f"Invoice {inv['invoice_number']} is {inv['status']!r}; only drafts are editable.")

    settable = {
        "client_name",
        "client_address", "client_city", "client_state", "client_zip",
        "client_email", "client_phone",
        "property_address", "property_city", "property_state", "property_zip",
        "due_at", "terms", "notes", "tax_rate", "contract_id",
    }
    payload: dict[str, Any] = {}
    for k, v in fields.items():
        if k in settable:
            payload[k] = v

    items = fields.get("line_items")
    if items is not None:
        items = coerce_line_items(items)
        if not items:
            raise ValueError("at least one line item is required")
        payload["line_items"] = json.dumps(items)
    else:
        items = json.loads(inv["line_items"])

    tax_rate = payload.get("tax_rate", inv["tax_rate"])
    subtotal, tax_amount, total = compute_totals(items, float(tax_rate or 0))
    payload["subtotal"]   = subtotal
    payload["tax_amount"] = tax_amount
    payload["total"]      = total
    payload["updated_at"] = _now()

    if not payload:
        return inv
    set_clause = ", ".join(f"{k} = :{k}" for k in payload.keys())
    payload["id"] = invoice_id
    with connect() as conn:
        conn.execute(f"UPDATE invoices SET {set_clause} WHERE id = :id", payload)
    return get_invoice(invoice_id)


def mark_sent(invoice_id: int) -> dict:
    inv = get_invoice(invoice_id)
    if inv["status"] != "draft":
        raise ValueError(f"Only drafts can be marked sent (current: {inv['status']}).")
    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE invoices SET status='sent', issued_at=COALESCE(issued_at, ?), updated_at=? WHERE id=?",
            (now, now, invoice_id),
        )
    return get_invoice(invoice_id)


def record_payment(
    invoice_id: int,
    *,
    amount: float,
    method: str | None = None,
    reference: str | None = None,
    paid_at: str | None = None,
) -> dict:
    """
    Apply a payment. Total status transitions to 'paid' when amount_paid >= total,
    'partial' otherwise. Cannot apply to drafts -- mark sent first.
    """
    inv = get_invoice(invoice_id)
    if inv["status"] in ("void",):
        raise ValueError("Cannot apply payment to a void invoice.")
    if inv["status"] == "draft":
        raise ValueError("Mark the invoice sent before recording a payment.")
    if amount is None or float(amount) <= 0:
        raise ValueError("Payment amount must be positive.")

    new_paid = round(float(inv["amount_paid"] or 0) + float(amount), 2)
    total = float(inv["total"] or 0)
    if new_paid + 0.005 >= total:
        new_status = "paid"
        paid_ts = paid_at or _now()
    else:
        new_status = "partial"
        paid_ts = inv["paid_at"]  # keep prior None or earlier paid_at unchanged

    now = _now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE invoices
               SET amount_paid       = ?,
                   status            = ?,
                   paid_at           = ?,
                   payment_method    = COALESCE(?, payment_method),
                   payment_reference = COALESCE(?, payment_reference),
                   updated_at        = ?
             WHERE id = ?
            """,
            (new_paid, new_status, paid_ts, method, reference, now, invoice_id),
        )
    return get_invoice(invoice_id)


def void_invoice(invoice_id: int, reason: str | None = None) -> dict:
    inv = get_invoice(invoice_id)
    if inv["status"] == "void":
        return inv
    now = _now()
    note = (inv["notes"] or "").rstrip()
    if reason:
        prefix = "\n" if note else ""
        note = f"{note}{prefix}[void {now}] {reason}"
    with connect() as conn:
        conn.execute(
            "UPDATE invoices SET status='void', notes=?, updated_at=? WHERE id=?",
            (note or None, now, invoice_id),
        )
    return get_invoice(invoice_id)


# ---------- Reads ----------

def _row_to_dict(row) -> dict:
    d = dict(row)
    # Derived: is this row overdue right now?
    d["is_overdue"] = bool(
        d.get("status") in ("sent", "partial")
        and d.get("due_at")
        and d["due_at"] < date.today().isoformat()
    )
    d["balance_due"] = round(float(d.get("total") or 0) - float(d.get("amount_paid") or 0), 2)
    return d


def get_invoice(invoice_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        raise LookupError(f"Invoice id={invoice_id} not found.")
    return _row_to_dict(row)


def get_invoice_by_number(number: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE invoice_number=?", (number,)).fetchone()
    return _row_to_dict(row) if row else None


def list_invoices(
    *,
    status: str | None = None,
    source: str | None = None,
    case_number: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Filterable list. status='overdue' returns derived-overdue rows (sent/partial with past due_at)."""
    sql = "SELECT * FROM invoices WHERE 1=1"
    params: list[Any] = []
    if status == "overdue":
        sql += " AND status IN ('sent','partial') AND due_at IS NOT NULL AND due_at < ?"
        params.append(date.today().isoformat())
    elif status:
        sql += " AND status = ?"
        params.append(status)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if case_number:
        sql += " AND case_number = ?"
        params.append(case_number)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def invoices_for_case(source: str, case_number: str) -> list[dict]:
    return list_invoices(source=source, case_number=case_number, limit=100)


def summary_stats() -> dict:
    """For the dashboard tile: counts + outstanding dollars."""
    today = date.today().isoformat()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status IN ('sent','partial')
                                 AND (due_at IS NULL OR due_at >= ?)
                                THEN total - amount_paid ELSE 0 END), 0) AS outstanding,
              COALESCE(SUM(CASE WHEN status IN ('sent','partial')
                                 AND due_at IS NOT NULL AND due_at < ?
                                THEN total - amount_paid ELSE 0 END), 0) AS overdue_amount,
              COALESCE(SUM(CASE WHEN status='paid' THEN total ELSE 0 END), 0) AS collected,
              SUM(CASE WHEN status='draft' THEN 1 ELSE 0 END)   AS drafts,
              SUM(CASE WHEN status='sent'  THEN 1 ELSE 0 END)   AS sent_count,
              SUM(CASE WHEN status='paid'  THEN 1 ELSE 0 END)   AS paid_count,
              SUM(CASE WHEN status IN ('sent','partial')
                        AND due_at IS NOT NULL AND due_at < ?
                       THEN 1 ELSE 0 END) AS overdue_count,
              COUNT(*) AS total_count
            FROM invoices
            """,
            (today, today, today),
        ).fetchone()
    return {
        "outstanding":   float(row["outstanding"]   or 0),
        "overdue_amount": float(row["overdue_amount"] or 0),
        "collected":     float(row["collected"]     or 0),
        "drafts":        int(row["drafts"]          or 0),
        "sent_count":    int(row["sent_count"]      or 0),
        "paid_count":    int(row["paid_count"]      or 0),
        "overdue_count": int(row["overdue_count"]   or 0),
        "total_count":   int(row["total_count"]     or 0),
    }


# ---------- Prefill from a contract event ----------

def prefill_from_case(source: str, case_number: str) -> dict:
    """
    Pull violation row + most recent contract pipeline_event for this case.
    Returns a dict the form template can splat onto its inputs.
    """
    with connect() as conn:
        v = conn.execute(
            "SELECT * FROM violations WHERE source=? AND case_number=?",
            (source, case_number),
        ).fetchone()
        ev = conn.execute(
            """
            SELECT * FROM pipeline_events
             WHERE source=? AND case_number=? AND event_type='contract'
             ORDER BY occurred_at DESC, id DESC LIMIT 1
            """,
            (source, case_number),
        ).fetchone()
        intake = conn.execute(
            """
            SELECT caller_name, caller_phone, caller_email
              FROM lead_intakes
             WHERE source=? AND case_number=?
             ORDER BY created_at DESC LIMIT 1
            """,
            (source, case_number),
        ).fetchone()

    if not v:
        raise LookupError(f"No violation row for {source}/{case_number}")

    contract_value = float(ev["contract_value"] or 0) if ev and ev["contract_value"] is not None else 0.0
    client_name  = (ev["contact_name"]  if ev and ev["contact_name"]  else None) \
                   or (intake["caller_name"]  if intake and intake["caller_name"]  else None) \
                   or v["owner_full_name"] or ""
    client_phone = (ev["contact_phone"] if ev and ev["contact_phone"] else None) \
                   or (intake["caller_phone"] if intake and intake["caller_phone"] else None)
    client_email = (ev["contact_email"] if ev and ev["contact_email"] else None) \
                   or (intake["caller_email"] if intake and intake["caller_email"] else None)

    return {
        "source":             source,
        "case_number":        case_number,
        "contract_event_id":  ev["id"] if ev else None,
        "client_name":        client_name,
        "client_address":     v["owner_mailing_address"],
        "client_phone":       client_phone,
        "client_email":       client_email,
        "property_address":   v["property_address"],
        "suggested_amount":   contract_value,
        "suggested_description": (
            f"Permit Solutions Services — case {case_number} "
            f"({v['property_address'] or 'property work'})"
        ),
    }
