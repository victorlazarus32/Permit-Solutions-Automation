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
from pathlib import Path
from typing import Iterable, Mapping, Any

DB_PATH = Path(__file__).resolve().parent / "data" / "violations.db"

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
