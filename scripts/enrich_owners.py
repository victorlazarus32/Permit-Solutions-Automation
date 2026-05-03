"""
Backfill owner_full_name and owner_mailing_address on rows that are flagged
NEEDS_OWNER_LOOKUP. Looks up each row's folio against the Miami-Dade
Property Appraiser and writes the result back.

Run:
    python -m scripts.enrich_owners                    # all rows in any source
    python -m scripts.enrich_owners --source homestead # one source
    python -m scripts.enrich_owners --limit 5          # smoke test first
    python -m scripts.enrich_owners --dry-run          # preview what would change

After this, those rows become eligible for the Lob mailer (the readiness
query in send.py excludes NEEDS_OWNER_LOOKUP).
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db import DB_PATH, init_db
from lookup.property_appraiser import lookup as pa_lookup

log = logging.getLogger("enrich_owners")


def _rows_needing_owner(source: str | None, limit: int | None) -> list[dict]:
    sql = """
        SELECT source, case_number, folio_number, property_address,
               owner_full_name, owner_mailing_address, comments
          FROM violations
         WHERE folio_number IS NOT NULL
           AND folio_number <> ''
           AND (
                owner_full_name      IS NULL OR owner_full_name      = ''
             OR owner_mailing_address IS NULL OR owner_mailing_address = ''
             OR comments LIKE '%NEEDS_OWNER_LOOKUP%'
           )
    """
    params: list = []
    if source:
        sql += " AND source = ?"; params.append(source)
    sql += " ORDER BY source, case_number"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _strip_needs_owner(comments: str | None) -> str | None:
    """Remove the NEEDS_OWNER_LOOKUP marker, preserving other notes."""
    if not comments:
        return None
    parts = [p.strip() for p in comments.split("|") if p.strip()
             and "NEEDS_OWNER_LOOKUP" not in p]
    return " | ".join(parts) if parts else None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true",
                   help="Preview changes without writing to the DB.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    init_db()

    rows = _rows_needing_owner(args.source, args.limit)
    log.info("%d row(s) need owner lookup", len(rows))
    if not rows:
        return 0

    enriched = 0
    failed = 0
    no_match = 0

    for r in rows:
        folio = (r["folio_number"] or "").strip()
        try:
            info = pa_lookup(folio)
        except Exception as e:
            log.warning("[%s/%s] PA lookup error for folio %s: %s",
                        r["source"], r["case_number"], folio, e)
            failed += 1
            continue

        if not info.found():
            log.info("[%s/%s] no PA hit for folio %s", r["source"],
                     r["case_number"], folio)
            no_match += 1
            continue

        owner = info.owner_full_name
        mail  = info.owner_mailing_address

        log.info("[%s/%s] %s  ->  %s  /  %s",
                 r["source"], r["case_number"], folio, owner, mail)

        if args.dry_run:
            enriched += 1
            continue

        new_comments = _strip_needs_owner(r["comments"])
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                UPDATE violations
                   SET owner_full_name       = ?,
                       owner_mailing_address = ?,
                       comments              = ?
                 WHERE source = ? AND case_number = ?
                """,
                (owner, mail, new_comments, r["source"], r["case_number"]),
            )
        enriched += 1

    log.info("=== summary ===")
    log.info("enriched: %d", enriched)
    log.info("no-match: %d", no_match)
    log.info("failed:   %d", failed)
    if args.dry_run:
        log.info("(dry run — DB was NOT modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
