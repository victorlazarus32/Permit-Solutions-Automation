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
