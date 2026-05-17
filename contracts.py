"""
Reusable contract templates.

A contract is a saved chunk of services-and-terms text (warranty language,
payment terms, punch-list clauses) that can be attached to invoices (and,
later, estimates) so the same boilerplate doesn't get retyped per job.

At most one contract may be flagged as the invoice default at a time, and
at most one as the estimate default. Setting one clears any prior flag of
the same kind — the toggle behaves like a radio button across the table.
"""
from __future__ import annotations

from datetime import datetime, timezone

from db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_contracts() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contracts ORDER BY name COLLATE NOCASE ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_contract(contract_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()
    return dict(row) if row else None


def get_default_invoice_contract() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM contracts WHERE is_default_invoice = 1 LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_default_estimate_contract() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM contracts WHERE is_default_estimate = 1 LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def create_contract(
    *,
    name: str,
    details: str | None = None,
    is_default_estimate: bool = False,
    is_default_invoice: bool = False,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Contract name is required.")
    now = _now()
    with connect() as conn:
        if is_default_estimate:
            conn.execute("UPDATE contracts SET is_default_estimate = 0")
        if is_default_invoice:
            conn.execute("UPDATE contracts SET is_default_invoice = 0")
        cur = conn.execute(
            """
            INSERT INTO contracts (
                name, details, is_default_estimate, is_default_invoice,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, details, 1 if is_default_estimate else 0,
             1 if is_default_invoice else 0, now, now),
        )
        new_id = cur.lastrowid
    return get_contract(new_id)  # type: ignore[return-value]


def update_contract(contract_id: int, **fields) -> dict:
    existing = get_contract(contract_id)
    if not existing:
        raise LookupError(f"Contract {contract_id} not found.")

    settable = {"name", "details", "is_default_estimate", "is_default_invoice"}
    updates = {k: v for k, v in fields.items() if k in settable}
    if "name" in updates:
        nm = (updates["name"] or "").strip()
        if not nm:
            raise ValueError("Contract name cannot be blank.")
        updates["name"] = nm
    for bkey in ("is_default_estimate", "is_default_invoice"):
        if bkey in updates:
            updates[bkey] = 1 if updates[bkey] else 0

    if not updates:
        return existing

    with connect() as conn:
        # Enforce single-default-per-kind across the table.
        if updates.get("is_default_estimate") == 1:
            conn.execute("UPDATE contracts SET is_default_estimate = 0 WHERE id <> ?",
                         (contract_id,))
        if updates.get("is_default_invoice") == 1:
            conn.execute("UPDATE contracts SET is_default_invoice = 0 WHERE id <> ?",
                         (contract_id,))
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        updates["id"] = contract_id
        conn.execute(f"UPDATE contracts SET {sets} WHERE id = :id", updates)
    return get_contract(contract_id)  # type: ignore[return-value]


def delete_contract(contract_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
