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


# ===== Status-transition auto-tasks =====
# When a job moves INTO any of these statuses, the listed tasks are auto-
# created on the job with a due date `due_in_days` from today. Each task
# description gets an "[auto]" prefix so it's visually distinct from
# manually-added tasks. Idempotent: if an OPEN auto-task with the same
# description already exists for this job, we don't duplicate.
STATUS_AUTO_TASKS: dict[str, list[dict]] = {
    "awaiting_survey": [
        {"description": "Email client requesting survey",                 "due_in_days": 1},
    ],
    "awaiting_engineer": [
        {"description": "Send case packet to engineer",                   "due_in_days": 2},
        {"description": "Follow up with engineer on ETA",                 "due_in_days": 5},
    ],
    "permit_prep": [
        {"description": "Compile permit application package",             "due_in_days": 3},
    ],
    "submitted": [
        {"description": "Follow up with city on review status",           "due_in_days": 5},
    ],
    "review_comments": [
        {"description": "Draft response to city review comments",         "due_in_days": 3},
    ],
    "awaiting_inspection": [
        {"description": "Confirm inspection date with client",            "due_in_days": 1},
    ],
    "inspection_failed": [
        {"description": "Send deficiency notice to client",               "due_in_days": 1},
        {"description": "Re-prepare submittal addressing deficiencies",   "due_in_days": 3},
    ],
    "approved": [
        {"description": "Send close-out package to client",               "due_in_days": 2},
        # NB: 'Generate closing invoice' used to live here as a manual task.
        # Phase 3 auto-creates a draft invoice instead — see
        # _auto_create_closing_invoice() called from transition_status().
        {"description": "Review + finalize the auto-created closing invoice", "due_in_days": 1},
    ],
    "closed_won": [
        {"description": "Email client thank-you + ask for Google review", "due_in_days": 1},
    ],
}

AUTO_PREFIX = "[auto] "


# Per-status "stuck" thresholds — how many days a job can sit in a status
# before it shows up on the "Stuck Jobs" widget. Terminal statuses are
# excluded entirely (a closed job can't be stuck).
STUCK_THRESHOLDS_DAYS: dict[str, int] = {
    "intake":               3,
    "awaiting_survey":      7,
    "awaiting_engineer":    10,
    "permit_prep":          5,
    "submitted":            14,
    "review_comments":      7,
    "awaiting_inspection":  14,
    "inspection_failed":    7,
    "approved":             7,
}


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
                      by: str | None = None, note: str | None = None,
                      skip_auto_tasks: bool = False) -> dict:
    """Move the job to a new status; append to status history; create any
    auto-tasks defined for the destination status (unless skip_auto_tasks)."""
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

    # Auto-create the tasks defined for this new status (after the status
    # itself is committed so subsequent reads see the new state).
    if not skip_auto_tasks:
        _seed_auto_tasks(job_id, to_status)
        # Phase 3: when a job is approved, draft the closing invoice
        # automatically (unless the job already has one linked).
        if to_status == "approved":
            _auto_create_closing_invoice(job_id)

    return get_job(job_id)  # type: ignore[return-value]


def _auto_create_closing_invoice(job_id: int) -> dict | None:
    """
    Create a draft 'closing' invoice prefilled from this job. Links the new
    invoice back to the job via jobs.invoice_id. Skips silently if the job
    already has an invoice linked.
    """
    job = get_job(job_id)
    if not job or job.get("invoice_id"):
        return None

    # Lazy imports — avoid pulling the invoice module into module-load path
    import invoices as inv_mod  # noqa: WPS433 (local import is intentional)

    # Parse property address into parts (street / city / state / zip) so
    # the printed invoice formats cleanly.
    parts = inv_mod._parse_address(job.get("property_address"))

    try:
        inv = inv_mod.create_invoice(
            client_name=job["client_name"],
            client_phone=job.get("client_phone"),
            client_email=job.get("client_email"),
            # Property address (full one-line value goes to the address field;
            # parsed parts go to city/state/zip).
            property_address=parts["street"] or job.get("property_address"),
            property_city=parts["city"],
            property_state=parts["state"],
            property_zip=parts["zip"],
            # Carry the violation linkage if there is one.
            source=job.get("source"),
            case_number=job.get("case_number"),
            # Placeholder line item — user fills in actual final-payment
            # amount before sending. create_invoice() requires >= 1 item.
            line_items=[{
                "description": f"Closing payment — {job['job_number']}",
                "quantity":    1,
                "unit_price":  0,
            }],
            notes=f"Auto-created on approval of {job['job_number']}. "
                  f"Fill in the final-payment amount before sending.",
        )
    except Exception:
        return None

    # Link invoice back to the job
    update_job(job_id, invoice_id=inv["id"])
    return inv


def _seed_auto_tasks(job_id: int, status_key: str) -> int:
    """Create auto-tasks for this status; skip any already present + open."""
    specs = STATUS_AUTO_TASKS.get(status_key) or []
    if not specs:
        return 0

    # Pull current open tasks so we don't double-create on re-transition.
    open_descs = {
        (t["description"] or "").strip()
        for t in list_tasks(job_id, include_completed=False)
    }

    created = 0
    today = date.today()
    for spec in specs:
        desc = f"{AUTO_PREFIX}{spec['description']}"
        if desc in open_descs:
            continue
        due = (today + _days_offset(spec.get("due_in_days") or 0)).isoformat()
        try:
            add_task(job_id, description=desc, due_at=due, assigned_to=None)
            created += 1
        except (ValueError, LookupError):
            pass
    return created


def _days_offset(n: int):
    """timedelta(n) — wrapped so the import lives in one place."""
    from datetime import timedelta
    return timedelta(days=int(n))


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


# ---------- Stuck-job detection ----------

def list_stuck_jobs(limit: int = 50) -> list[dict]:
    """
    Return non-terminal jobs that have been in their current status longer
    than STUCK_THRESHOLDS_DAYS for that status. Each result dict includes:
        + entered_status_at  — when this status was entered (ISO)
        + days_in_status     — int, full days since entered
        + threshold_days     — the stuck threshold for this status

    Sorted by days_in_status DESC so the most-stuck float to the top.
    """
    from datetime import datetime as _dt
    with connect() as conn:
        # For each job, find when it entered its CURRENT status. Pick the
        # most recent history row whose to_status matches the job's status.
        rows = conn.execute(
            """
            SELECT j.*,
                   (SELECT MAX(h.transitioned_at) FROM job_status_history h
                     WHERE h.job_id = j.id AND h.to_status = j.status) AS entered_status_at
              FROM jobs j
             WHERE j.status NOT IN ('closed_won', 'closed_lost')
            """
        ).fetchall()

    today = _dt.now(timezone.utc)
    out: list[dict] = []
    for r in rows:
        threshold = STUCK_THRESHOLDS_DAYS.get(r["status"])
        if threshold is None:
            continue
        entered = r["entered_status_at"] or r["opened_at"]
        if not entered:
            continue
        try:
            entered_dt = _dt.fromisoformat(entered.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        days = (today - entered_dt).days
        if days < threshold:
            continue
        d = dict(r)
        d["entered_status_at"] = entered
        d["days_in_status"] = days
        d["threshold_days"] = threshold
        out.append(d)

    out.sort(key=lambda d: d["days_in_status"], reverse=True)
    return out[:limit]


# ---------- Internal helpers ----------

def _get_task(task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_tasks WHERE id = ?",
                           (task_id,)).fetchone()
    return dict(row) if row else None
