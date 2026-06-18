"""
SQLite database for the violation pipeline.

One table holds violations from ALL municipalities, normalized into the same
schema. The 'source' column tells you which connector wrote the row.

Why SQLite: zero setup, one file, easy to back up, perfectly fine for tens of
thousands of rows. Migrate to Postgres later if volume demands it.

Idempotency: upserts on (source, case_number). Re-running a connector for the
same date range will not create duplicates — it will refresh the row.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterable, Mapping, Any

# DB lives in the project's data/ folder by default. On Render (or any host
# with a persistent disk mounted elsewhere), set DB_PATH to that mount point
# so the database survives deploys.
_default_db_path = Path(__file__).resolve().parent / "data" / "violations.db"
DB_PATH = Path(os.environ.get("DB_PATH") or _default_db_path)

SCHEMA = """
CREATE TABLE IF NOT EXISTS violations (
    -- Identity
    source              TEXT NOT NULL,           -- e.g. 'miami_dade_unincorporated'
    case_number         TEXT NOT NULL,

    -- Case metadata
    case_type           TEXT,
    open_date           TEXT,                    -- ISO date
    close_date          TEXT,
    activity_date       TEXT,
    activity            TEXT,
    inspector           TEXT,
    deputy_clerk        TEXT,
    permit_number       TEXT,
    building_code       TEXT,
    district_number     TEXT,

    -- Property
    property_address    TEXT,
    folio_number        TEXT,
    legal_description   TEXT,

    -- Owner / mailing
    owner_full_name     TEXT,
    owner_mailing_address TEXT,
    violator            TEXT,

    -- Violation narrative
    alleged_violation   TEXT,
    comments            TEXT,

    -- Filter audit
    matched_keywords    TEXT,                    -- comma-separated list

    -- Pipeline state
    first_seen_at       TEXT NOT NULL,           -- ISO timestamp of first ingest
    last_seen_at        TEXT NOT NULL,           -- ISO timestamp of latest re-ingest
    raw_source_file     TEXT,                    -- path to archived .xls

    -- Mailing lifecycle (filled by Lob stage, NULL until then)
    lob_letter_id       TEXT,
    lob_status          TEXT,                    -- created / mailed / in_transit / delivered / returned / failed
    lob_mailed_at       TEXT,
    lob_delivered_at    TEXT,
    lob_last_event_at   TEXT,

    PRIMARY KEY (source, case_number)
);

CREATE INDEX IF NOT EXISTS ix_violations_source_open ON violations(source, open_date);
CREATE INDEX IF NOT EXISTS ix_violations_lob_status ON violations(lob_status);
CREATE INDEX IF NOT EXISTS ix_violations_folio ON violations(folio_number);

-- Pipeline events: per-case CRM trail. Every call, text, contract, no-show, etc.
-- One violation row can have many events. The dashboard funnel reads from here.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    case_number     TEXT NOT NULL,
    event_type      TEXT NOT NULL,            -- 'call' | 'text' | 'email' | 'meeting' | 'contract' | 'declined' | 'no_response' | 'note'
    occurred_at     TEXT NOT NULL,            -- ISO date or datetime when the event happened
    contact_name    TEXT,
    contact_phone   TEXT,
    contact_email   TEXT,
    contract_value  REAL,                     -- only meaningful for event_type='contract'
    notes           TEXT,
    created_at      TEXT NOT NULL,            -- ISO timestamp this row was logged
    FOREIGN KEY (source, case_number) REFERENCES violations(source, case_number)
);

CREATE INDEX IF NOT EXISTS ix_pipeline_case  ON pipeline_events(source, case_number);
CREATE INDEX IF NOT EXISTS ix_pipeline_type  ON pipeline_events(event_type);
CREATE INDEX IF NOT EXISTS ix_pipeline_occur ON pipeline_events(occurred_at DESC);

-- Lead intakes: rich qualification record per inbound contact.
-- BANT-style + property-specific. Score is auto-calculated from inputs.
-- Senior review (status pending / approved / rejected) gates whether the
-- lead gets worked.
CREATE TABLE IF NOT EXISTS lead_intakes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source                   TEXT NOT NULL,
    case_number              TEXT NOT NULL,

    -- Call metadata
    call_at                  TEXT NOT NULL,
    inbound_channel          TEXT,         -- phone | text | email | web | walkin
    caller_name              TEXT,
    caller_phone             TEXT,
    caller_email             TEXT,
    best_callback_time       TEXT,

    -- Authority
    is_property_owner        INTEGER,      -- 0/1
    relationship_to_owner    TEXT,         -- spouse | family | tenant | attorney | agent | other
    has_permission           INTEGER,

    -- Violation status (what they have on hand right now)
    notices_received_count   INTEGER,
    fines_accrued_usd        REAL,
    lien_filed               INTEGER,
    court_date               TEXT,
    inspector_contact        TEXT,

    -- Motivation & urgency
    primary_motivation       TEXT,         -- avoid_lien | sale | refi | compliance | fines | family | other
    urgency                  TEXT,         -- critical | high | medium | low
    has_tried_diy            INTEGER,
    has_contacted_city       INTEGER,
    has_hired_before         INTEGER,
    previous_contractor      TEXT,

    -- Scope of work
    violation_types          TEXT,         -- comma-separated tags
    materials                TEXT,         -- comma-separated
    rough_linear_feet        REAL,
    originally_permitted     INTEGER,
    currently_standing       INTEGER,

    -- Money & decision
    decision_maker           TEXT,         -- self | spouse | family | partner | hoa | other
    budget_aware             INTEGER,
    has_other_quotes         INTEGER,
    other_quotes_from        TEXT,
    insurance_involved       INTEGER,

    -- Timeline
    target_resolution_date   TEXT,
    timeline_flexibility     TEXT,         -- flexible | somewhat | inflexible
    deadline_reason          TEXT,

    -- Operator assessment
    lead_temperature         TEXT,         -- hot | warm | cold (auto-derived from score)
    lead_score               INTEGER,      -- 0..100
    disposition              TEXT,         -- book_consult | send_quote | send_info | followup | not_qualified | needs_research | contract_signed
    contract_value_usd       REAL,         -- if disposition = contract_signed
    next_action              TEXT,
    next_action_at           TEXT,
    assigned_to              TEXT,
    operator_notes           TEXT,

    -- Senior review
    senior_review_status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    senior_reviewer          TEXT,
    senior_review_notes      TEXT,
    senior_reviewed_at       TEXT,

    created_at               TEXT NOT NULL,
    updated_at               TEXT,

    FOREIGN KEY (source, case_number) REFERENCES violations(source, case_number)
);

CREATE INDEX IF NOT EXISTS ix_intakes_case   ON lead_intakes(source, case_number);
CREATE INDEX IF NOT EXISTS ix_intakes_review ON lead_intakes(senior_review_status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_intakes_score  ON lead_intakes(lead_score DESC);

-- Invoices: one row per invoice issued by PSS. Tied back to a violation
-- (source, case_number) when the work came out of the lead pipeline, and
-- optionally to the pipeline_event that captured the signed contract.
-- Line items live as JSON so we don't need a separate child table for v1.
CREATE TABLE IF NOT EXISTS invoices (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number       TEXT NOT NULL UNIQUE,    -- e.g. PSS-2026-0001
    source               TEXT,                    -- nullable: invoice may not tie to a case
    case_number          TEXT,
    contract_event_id    INTEGER,                 -- pipeline_events.id when generated from a contract

    -- Bill-to
    client_name          TEXT NOT NULL,
    client_address       TEXT,                    -- mailing street (line 1)
    client_city          TEXT,
    client_state         TEXT,
    client_zip           TEXT,
    client_email         TEXT,
    client_phone         TEXT,

    -- Property the work concerns (often == case property; explicit so manual
    -- invoices outside the lead flow still capture it)
    property_address     TEXT,                    -- property street (line 1)
    property_city        TEXT,
    property_state       TEXT,
    property_zip         TEXT,

    -- Money
    line_items           TEXT NOT NULL,           -- JSON: [{description, quantity, unit_price, amount}, ...]
    subtotal             REAL NOT NULL DEFAULT 0,
    tax_rate             REAL NOT NULL DEFAULT 0, -- e.g. 0.07 for 7%
    tax_amount           REAL NOT NULL DEFAULT 0,
    total                REAL NOT NULL DEFAULT 0,
    amount_paid          REAL NOT NULL DEFAULT 0,

    -- Lifecycle
    status               TEXT NOT NULL DEFAULT 'draft',   -- draft | sent | paid | partial | overdue | void
    issued_at            TEXT,                    -- when status moved to 'sent'
    due_at               TEXT,                    -- ISO date
    paid_at              TEXT,                    -- when fully paid
    payment_method       TEXT,                    -- 'zelle' | 'check' | 'cash' | 'card' | 'other'
    payment_reference    TEXT,                    -- check #, txn id, etc.

    -- Free-form
    notes                TEXT,                    -- internal notes (NOT printed)
    terms                TEXT,                    -- printed on invoice ("Net 30", "Due on receipt")

    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,

    FOREIGN KEY (source, case_number) REFERENCES violations(source, case_number),
    FOREIGN KEY (contract_event_id)   REFERENCES pipeline_events(id)
);

CREATE INDEX IF NOT EXISTS ix_invoices_status   ON invoices(status, due_at);
CREATE INDEX IF NOT EXISTS ix_invoices_case     ON invoices(source, case_number);
CREATE INDEX IF NOT EXISTS ix_invoices_number   ON invoices(invoice_number);
CREATE INDEX IF NOT EXISTS ix_invoices_created  ON invoices(created_at DESC);

-- Reusable contract templates (services description + terms & conditions).
-- One can be marked as the default for new estimates and/or new invoices.
CREATE TABLE IF NOT EXISTS contracts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    details              TEXT,                    -- full body: services + terms + warranties
    is_default_estimate  INTEGER NOT NULL DEFAULT 0,
    is_default_invoice   INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_contracts_name ON contracts(name);

-- Reusable scope-of-services module blocks. Stored modularly so an invoice's
-- "Scope of Services" section is assembled from selected modules with
-- per-job variable substitution ({{jurisdiction}}, {{fence_type}}, etc.).
CREATE TABLE IF NOT EXISTS scope_modules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL UNIQUE,    -- machine slug, e.g. 'compliance_review'
    name         TEXT NOT NULL,           -- human label, e.g. 'Compliance Review'
    body         TEXT,                    -- the actual text, may contain {{vars}}
    category     TEXT,                    -- 'fence', 'permit', 'general', etc.
    sort_order   INTEGER NOT NULL DEFAULT 100,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_scope_modules_category ON scope_modules(category, sort_order);

-- ===== Jobs (operational workflow / mini-CRM) =====
-- A Job is the engagement with a client for a specific permit case from
-- intake through close-out. It ties a violation lead + invoices + tasks +
-- status history together so we can see the entire lifecycle in one place.
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_number          TEXT NOT NULL UNIQUE,            -- PSS-J-2026-0001
    -- Links to other entities (all nullable — a job may stand alone)
    source              TEXT,
    case_number         TEXT,
    invoice_id          INTEGER,
    -- Client + property snapshot
    client_name         TEXT NOT NULL,
    client_phone        TEXT,
    client_email        TEXT,
    property_address    TEXT,
    -- Workflow
    status              TEXT NOT NULL DEFAULT 'intake',
    -- Lifecycle
    opened_at           TEXT NOT NULL,
    closed_at           TEXT,
    -- Free-form
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (source, case_number) REFERENCES violations(source, case_number),
    FOREIGN KEY (invoice_id)          REFERENCES invoices(id)
);

CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_case   ON jobs(source, case_number);

CREATE TABLE IF NOT EXISTS job_status_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL,
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    transitioned_at TEXT NOT NULL,
    transitioned_by TEXT,
    note            TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS ix_job_status_hist ON job_status_history(job_id, transitioned_at DESC);

CREATE TABLE IF NOT EXISTS job_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL,
    description     TEXT NOT NULL,
    due_at          TEXT,
    assigned_to     TEXT,
    completed_at    TEXT,
    completed_by    TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS ix_job_tasks_open ON job_tasks(job_id, completed_at);

-- ===== Workflow on invoices (formerly the Jobs feature) =====
-- We unified Jobs into Invoices: every engagement is now a single Invoice
-- record that carries BOTH billing state (status: draft/sent/paid/void)
-- AND workflow state (workflow_status: intake/permit_prep/submitted/etc.).
-- These two tables are the invoice-keyed counterparts of the now-deprecated
-- job_tasks and job_status_history.
CREATE TABLE IF NOT EXISTS invoice_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER NOT NULL,
    description     TEXT NOT NULL,
    due_at          TEXT,
    assigned_to     TEXT,
    completed_at    TEXT,
    completed_by    TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE INDEX IF NOT EXISTS ix_invoice_tasks_open ON invoice_tasks(invoice_id, completed_at);

CREATE TABLE IF NOT EXISTS invoice_workflow_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER NOT NULL,
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    transitioned_at TEXT NOT NULL,
    transitioned_by TEXT,
    note            TEXT,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE INDEX IF NOT EXISTS ix_invoice_workflow_hist ON invoice_workflow_history(invoice_id, transitioned_at DESC);

-- ===== Daily-run audit trail =====
-- One row per execution of scripts/daily_run.py (the Render cron job).
-- Lets the operator see at a glance "did the morning run actually fire,
-- what did it pull, what did it send" without trawling Render logs.
CREATE TABLE IF NOT EXISTS daily_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    status               TEXT NOT NULL,            -- 'success' | 'partial' | 'failed'
    homestead_pulled     INTEGER NOT NULL DEFAULT 0,    -- raw rows Tyler returned
    homestead_in_scope   INTEGER NOT NULL DEFAULT 0,
    homestead_inserted   INTEGER NOT NULL DEFAULT 0,
    md_pulled            INTEGER NOT NULL DEFAULT 0,    -- raw rows MD scraper returned
    md_in_scope          INTEGER NOT NULL DEFAULT 0,
    md_inserted          INTEGER NOT NULL DEFAULT 0,
    pinecrest_pulled     INTEGER NOT NULL DEFAULT 0,    -- raw rows eTRAKiT returned
    pinecrest_in_scope   INTEGER NOT NULL DEFAULT 0,
    pinecrest_inserted   INTEGER NOT NULL DEFAULT 0,
    palmetto_bay_pulled  INTEGER NOT NULL DEFAULT 0,    -- raw rows Eden returned
    palmetto_bay_in_scope INTEGER NOT NULL DEFAULT 0,
    palmetto_bay_inserted INTEGER NOT NULL DEFAULT 0,
    letters_eligible     INTEGER NOT NULL DEFAULT 0,
    letters_sent         INTEGER NOT NULL DEFAULT 0,
    letters_skipped      INTEGER NOT NULL DEFAULT 0,
    letters_failed       INTEGER NOT NULL DEFAULT 0,
    auto_send_enabled    INTEGER NOT NULL DEFAULT 0,
    error_text           TEXT,                      -- top-level traceback if status != success
    summary_text         TEXT                       -- the human-readable report we'd email
);

CREATE INDEX IF NOT EXISTS ix_daily_runs_started ON daily_runs(started_at DESC);

-- ===== Public-records request registry =====
-- One row per PRR ever submitted to a city. Tracks the date window each
-- request covered so the next request can start exactly where the prior
-- one stopped — no gap, no overlap. The /prr page reads from here.
CREATE TABLE IF NOT EXISTS prr_requests (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    city                 TEXT NOT NULL,            -- matches connector source key
    reference_number     TEXT,                     -- e.g. 'PRR-2026-144', '26-3395'
    security_key         TEXT,                     -- JustFOIA-style secondary auth code
    portal_url           TEXT,                     -- frozen at submit time
    custodian_email      TEXT,
    custodian_phone      TEXT,
    submitted_at         TEXT NOT NULL,            -- ISO YYYY-MM-DD
    covers_from          TEXT,                     -- ISO YYYY-MM-DD; first day of requested window
    covers_through       TEXT,                     -- ISO YYYY-MM-DD; last day of requested window
    status               TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'fulfilled' | 'no_records' | 'declined'
    fulfilled_at         TEXT,                     -- ISO date when response (Excel or denial) was received
    excel_uploaded_at    TEXT,                     -- ISO timestamp when the response Excel was processed via /upload-prr
    reminder_routine_id  TEXT,                     -- e.g. 'trig_xxx' for the scheduled status-check routine
    notes                TEXT,                     -- free-text operator notes
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_prr_requests_city_submitted
    ON prr_requests(city, submitted_at DESC);
CREATE INDEX IF NOT EXISTS ix_prr_requests_status
    ON prr_requests(status);
"""


@contextmanager
def connect():
    """Context-managed SQLite connection with row factory."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_lead_intakes(conn)
        _migrate_invoices(conn)
        _migrate_violations(conn)
        _migrate_daily_runs(conn)


def _migrate_daily_runs(conn) -> None:
    """Idempotent column adds for the daily_runs audit table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_runs)")}
    additions = [
        ("pinecrest_pulled",      "INTEGER NOT NULL DEFAULT 0"),
        ("pinecrest_in_scope",    "INTEGER NOT NULL DEFAULT 0"),
        ("pinecrest_inserted",    "INTEGER NOT NULL DEFAULT 0"),
        ("palmetto_bay_pulled",   "INTEGER NOT NULL DEFAULT 0"),
        ("palmetto_bay_in_scope", "INTEGER NOT NULL DEFAULT 0"),
        ("palmetto_bay_inserted", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, ddl in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE daily_runs ADD COLUMN {col} {ddl}")


def _migrate_violations(conn) -> None:
    """Idempotent column adds for the violations table (Lob address verification)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(violations)")}
    additions = [
        ("lob_address_deliverability", "TEXT"),  # 'deliverable' | 'undeliverable' | etc.
        ("lob_address_verified_at",    "TEXT"),  # ISO timestamp of last verify call
        ("lob_address_verify_error",   "TEXT"),  # last error string when verify failed
        # Operator soft-skip: row stays in DB for history/reporting but never
        # appears in the mail queue and is never sent by Lob.
        ("do_not_mail",                "INTEGER NOT NULL DEFAULT 0"),
        ("do_not_mail_reason",         "TEXT"),  # free-text why (operator note)
        ("do_not_mail_at",             "TEXT"),  # ISO timestamp when flagged
    ]
    for col, ddl in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE violations ADD COLUMN {col} {ddl}")


def _migrate_lead_intakes(conn) -> None:
    """
    Idempotent column adds for the lead_intakes table. SQLite supports
    ALTER TABLE ADD COLUMN, so each new field becomes one statement.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(lead_intakes)")}
    additions = [
        ("lead_source",             "TEXT"),
        ("caller_property_address", "TEXT"),
        ("caller_jurisdiction",     "TEXT"),
    ]
    for col, ddl in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE lead_intakes ADD COLUMN {col} {ddl}")


def _migrate_invoices(conn) -> None:
    """Idempotent column adds for the invoices table."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(invoices)")}
    additions = [
        ("client_city",    "TEXT"),
        ("client_state",   "TEXT"),
        ("client_zip",     "TEXT"),
        ("property_city",  "TEXT"),
        ("property_state", "TEXT"),
        ("property_zip",   "TEXT"),
        ("contract_id",    "INTEGER"),                    # FK -> contracts.id, nullable
        ("permit_number",  "TEXT"),                        # municipal permit number for the job, entered at intake
        ("proposal_data",  "TEXT"),                        # JSON: structured proposal/agreement inputs (properties, permit rows, fee, terms) so the branded PDF can be regenerated on demand
        ("costs",          "TEXT"),                        # JSON: list of internal overhead costs [{description, amount}] subtracted from profit (e.g. as-built letter $250)
        ("deposit_amount", "REAL NOT NULL DEFAULT 0"),    # dollars due as a deposit (0 = none)
        ("deposit_paid_at",   "TEXT"),                    # date the deposit was collected (YYYY-MM-DD)
        ("deposit_method",    "TEXT"),                    # how the deposit was paid: zelle/check/cash/card/other
        ("deposit_reference", "TEXT"),                    # optional reference for the deposit payment
        ("scope_of_services", "TEXT"),                    # assembled scope text for this invoice
        ("client_summary",    "TEXT"),                    # plain-English "what this means" blurb for the client
        ("workflow_status",   "TEXT NOT NULL DEFAULT 'intake'"),  # permit workflow stage (formerly the Job's status)
        ("workflow_opened_at","TEXT"),                    # when this engagement entered the workflow
        ("workflow_closed_at","TEXT"),                    # when the workflow reached a terminal state
        ("owner",             "TEXT"),                    # username of the operator who owns this invoice (NULL = legacy/unassigned)
    ]
    for col, ddl in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {ddl}")
    # Backfill workflow_opened_at for invoices that pre-date the workflow
    # feature — use the invoice's created_at so reports have a sensible
    # "in this status for N days" baseline.
    conn.execute(
        "UPDATE invoices SET workflow_opened_at = created_at "
        "WHERE workflow_opened_at IS NULL"
    )
    # Backfill owner for invoices that pre-date the access-control feature.
    # Assign to 'victor' (admin) so admins keep full visibility and the rows
    # can be reassigned from the invoice detail page after rollout.
    conn.execute("UPDATE invoices SET owner = 'victor' WHERE owner IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_invoices_owner ON invoices(owner)")


# Columns we accept on upsert (everything except lob_* which is set later)
_UPSERT_COLS = [
    "source", "case_number",
    "case_type", "open_date", "close_date", "activity_date", "activity",
    "inspector", "deputy_clerk", "permit_number", "building_code", "district_number",
    "property_address", "folio_number", "legal_description",
    "owner_full_name", "owner_mailing_address", "violator",
    "alleged_violation", "comments",
    "matched_keywords",
    "first_seen_at", "last_seen_at", "raw_source_file",
]


def upsert_violations(rows: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    """
    Insert or update violations. Returns (inserted, updated).

    On conflict (source, case_number):
      - last_seen_at, raw_source_file, and any narrative fields refresh
      - first_seen_at is preserved
      - lob_* fields are NEVER touched here
    """
    rows = list(rows)
    if not rows:
        return (0, 0)

    placeholders = ", ".join(f":{c}" for c in _UPSERT_COLS)
    cols = ", ".join(_UPSERT_COLS)

    # On conflict: update everything except first_seen_at and the lob_* fields
    update_cols = [c for c in _UPSERT_COLS if c not in ("source", "case_number", "first_seen_at")]
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    sql = f"""
        INSERT INTO violations ({cols})
        VALUES ({placeholders})
        ON CONFLICT(source, case_number) DO UPDATE SET
            {update_clause}
    """

    inserted = 0
    updated = 0
    with connect() as conn:
        for row in rows:
            # Check if row exists to count insert vs update
            cur = conn.execute(
                "SELECT 1 FROM violations WHERE source=? AND case_number=?",
                (row["source"], row["case_number"]),
            )
            existed = cur.fetchone() is not None
            conn.execute(sql, row)
            if existed:
                updated += 1
            else:
                inserted += 1
    return (inserted, updated)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
