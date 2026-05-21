"""
Jobs — the operational workflow layer.

A Job is the engagement with a client for a specific permit case, from
intake through close-out. It ties together a violation lead + the invoices
billed for that engagement + the tasks pending + the status history, so
the entire lifecycle of an engagement is visible in one place.

Numbering follows PSS-J-YYYY-NNNN, parallel to PSS-YYYY-NNNN for invoices.

Status machine (the user can transition to any other status — guardrails
are operational hints, not hard locks):

    intake               -> awaiting_survey | awaiting_engineer | permit_prep | closed_lost
    awaiting_survey      -> permit_prep | awaiting_engineer | closed_lost
    awaiting_engineer    -> permit_prep | awaiting_survey | closed_lost
    permit_prep          -> submitted | awaiting_survey | awaiting_engineer | closed_lost
    submitted            -> review_comments | awaiting_inspection | closed_lost
    review_comments      -> submitted | permit_prep | closed_lost
    awaiting_inspection  -> inspection_failed | approved | closed_lost
    inspection_failed    -> permit_prep | awaiting_inspection | closed_lost
    approved             -> closed_won | inspection_failed
    closed_won           -> (terminal)
    closed_lost          -> (terminal)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, timezone

from db import connect


# Canonical status set + human labels for the UI.
STATUSES: list[tuple[str, str]] = [
    ("intake",              "Intake"),
    ("awaiting_survey",     "Awaiting Survey"),
    ("awaiting_engineer",   "Awaiting Engineer"),
    ("permit_prep",         "Permit Prep"),
    ("submitted",           "Submitted"),
    ("review_comments",     "Review Comments"),
    ("awaiting_inspection", "Awaiting Inspection"),
    ("inspection_failed",   "Inspection Failed"),
    ("approved",            "Approved"),
    ("closed_won",          "Closed (Won)"),
    ("closed_lost",         "Closed (Lost)"),
]
STATUS_LABEL = dict(STATUSES)
STATUS_KEYS  = [k for k, _ in STATUSES]

# Statuses that mean "this job is done, no more action needed".
TERMINAL_STATUSES = {"closed_won", "closed_lost"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Numbering ----------

def next_job_number(conn, year: int | None = None) -> str:
    """PSS-J-YYYY-NNNN. NNNN is the next per-year sequence."""
    year = year or date.today().year
    prefix = f"PSS-J-{year}-"
    row = conn.execute(
        "SELECT job_number FROM jobs WHERE job_number LIKE ? "
        "ORDER BY job_number DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = 1
    if row:
        try:
            seq = int(row[0].split("-")[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f"{prefix}{seq:04d}"


# ---------- Jobs CRUD ----------

def list_jobs(status: str | None = None, limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM jobs"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY status ASC, updated_at DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_job_by_number(job_number: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_number = ?",
                           (job_number,)).fetchone()
    return dict(row) if row else None


def get_job_for_case(source: str, case_number: str) -> dict | None:
    """Return the most-recent job tied to this violation, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND case_number = ? "
            "ORDER BY opened_at DESC LIMIT 1",
            (source, case_number),
        ).fetchone()
    return dict(row) if row else None


def status_counts() -> dict:
    """{status_key: count} for everything not terminal — for dashboard stats."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def create_job(
    *,
    client_name: str,
    source: str | None = None,
    case_number: str | None = None,
    invoice_id: int | None = None,
    client_phone: str | None = None,
    client_email: str | None = None,
    property_address: str | None = None,
    notes: str | None = None,
    initial_status: str = "intake",
    initial_by: str | None = None,
) -> dict:
    client_name = (client_name or "").strip()
    if not client_name:
        raise ValueError("Client name is required.")
    if initial_status not in STATUS_LABEL:
        raise ValueError(f"Unknown status: {initial_status!r}")

    now = _now()
    with connect() as conn:
        number = next_job_number(conn)
        cur = conn.execute(
            """
            INSERT INTO jobs (
                job_number, source, case_number, invoice_id,
                client_name, client_phone, client_email, property_address,
                status, opened_at, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (number, source, case_number, invoice_id,
             client_name, client_phone, client_email, property_address,
             initial_status, now, notes, now, now),
        )
        new_id = cur.lastrowid
        # Seed history with the initial state.
        conn.execute(
            "INSERT INTO job_status_history "
            "(job_id, from_status, to_status, transitioned_at, transitioned_by, note) "
            "VALUES (?, NULL, ?, ?, ?, ?)",
            (new_id, initial_status, now, initial_by, "Job created"),
        )
    return get_job(new_id)  # type: ignore[return-value]


def update_job(job_id: int, **fields) -> dict:
    existing = get_job(job_id)
    if not existing:
        raise LookupError(f"Job {job_id} not found.")
    settable = {
        "client_name", "client_phone", "client_email",
        "property_address", "notes", "invoice_id",
    }
    updates = {k: v for k, v in fields.items() if k in settable}
    if not updates:
        return existing
    updates["updated_at"] = _now()
    with connect() as conn:
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        updates["id"] = job_id
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = :id", updates)
    return get_job(job_id)  # type: ignore[return-value]


def transition_status(job_id: int, *, to_status: str,
                      by: str | None = None, note: str | None = None) -> dict:
    """Move the job to a new status; append to status history."""
    if to_status not in STATUS_LABEL:
        raise ValueError(f"Unknown status: {to_status!r}")
    existing = get_job(job_id)
    if not existing:
        raise LookupError(f"Job {job_id} not found.")
    from_status = existing["status"]
    if from_status == to_status:
        return existing  # idempotent

    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_status_history "
            "(job_id, from_status, to_status, transitioned_at, transitioned_by, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, from_status, to_status, now, by, note),
        )
        closed_at_sql = ", closed_at = :now" if to_status in TERMINAL_STATUSES else ""
        # Open back up if transitioning out of a terminal state.
        if from_status in TERMINAL_STATUSES and to_status not in TERMINAL_STATUSES:
            closed_at_sql = ", closed_at = NULL"
        conn.execute(
            f"UPDATE jobs SET status = :s, updated_at = :now {closed_at_sql} "
            f"WHERE id = :id",
            {"s": to_status, "now": now, "id": job_id},
        )
    return get_job(job_id)  # type: ignore[return-value]


def list_status_history(job_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM job_status_history WHERE job_id = ? "
            "ORDER BY transitioned_at DESC",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Tasks ----------

def list_tasks(job_id: int, *, include_completed: bool = True) -> list[dict]:
    sql = "SELECT * FROM job_tasks WHERE job_id = ?"
    if not include_completed:
        sql += " AND completed_at IS NULL"
    sql += " ORDER BY (completed_at IS NOT NULL), COALESCE(due_at, created_at) ASC"
    with connect() as conn:
        rows = conn.execute(sql, (job_id,)).fetchall()
    return [dict(r) for r in rows]


def list_open_tasks_all(limit: int = 200) -> list[dict]:
    """All open tasks across all jobs — for a daily 'what needs doing' view."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.*, j.job_number, j.client_name, j.status AS job_status
              FROM job_tasks t
              JOIN jobs j ON j.id = t.job_id
             WHERE t.completed_at IS NULL
             ORDER BY COALESCE(t.due_at, t.created_at) ASC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def add_task(job_id: int, *, description: str,
             due_at: str | None = None, assigned_to: str | None = None) -> dict:
    description = (description or "").strip()
    if not description:
        raise ValueError("Task description is required.")
    if not get_job(job_id):
        raise LookupError(f"Job {job_id} not found.")
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO job_tasks (job_id, description, due_at, assigned_to, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, description, due_at or None, assigned_to or None, now),
        )
        task_id = cur.lastrowid
        # Touch the job so it bubbles up in updated_at-sorted lists.
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now, job_id))
    return _get_task(task_id)  # type: ignore[return-value]


def complete_task(task_id: int, *, by: str | None = None) -> dict:
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE job_tasks SET completed_at = ?, completed_by = ? "
            "WHERE id = ? AND completed_at IS NULL",
            (now, by, task_id),
        )
        if cur.rowcount == 0:
            existing = _get_task(task_id)
            if not existing:
                raise LookupError(f"Task {task_id} not found.")
            return existing  # already completed, no-op
    return _get_task(task_id)  # type: ignore[return-value]


def reopen_task(task_id: int) -> dict:
    with connect() as conn:
        conn.execute(
            "UPDATE job_tasks SET completed_at = NULL, completed_by = NULL "
            "WHERE id = ?",
            (task_id,),
        )
    return _get_task(task_id)  # type: ignore[return-value]


def delete_task(task_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM job_tasks WHERE id = ?", (task_id,))


def _get_task(task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_tasks WHERE id = ?",
                           (task_id,)).fetchone()
    return dict(row) if row else None
