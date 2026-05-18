"""
Diagnostic: show why rows in the queue aren't getting mailed.

Buckets the unsent rows by what's missing (owner name, mailing address,
NEEDS_OWNER_LOOKUP flag) and shows a sample of the first few unsent rows
in full detail.

Usage:
    python -m scripts.queue_status
"""
from __future__ import annotations

import sqlite3

from db import DB_PATH


def main() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        print(f"DB: {DB_PATH}\n")

        print("Unsent by source:")
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM violations "
            "WHERE lob_letter_id IS NULL GROUP BY source"
        ).fetchall()
        if not rows:
            print("  (no unsent rows)")
        for r in rows:
            print(f"  {r['source']}: {r['n']}")
        print()

        no_owner = conn.execute(
            "SELECT COUNT(*) FROM violations "
            "WHERE lob_letter_id IS NULL "
            "AND (owner_full_name IS NULL OR owner_full_name = '')"
        ).fetchone()[0]
        no_mail = conn.execute(
            "SELECT COUNT(*) FROM violations "
            "WHERE lob_letter_id IS NULL "
            "AND (owner_mailing_address IS NULL OR owner_mailing_address = '')"
        ).fetchone()[0]
        needs = conn.execute(
            "SELECT COUNT(*) FROM violations "
            "WHERE lob_letter_id IS NULL "
            "AND comments LIKE '%NEEDS_OWNER_LOOKUP%'"
        ).fetchone()[0]

        print("Why those rows aren't letter-ready:")
        print(f"  Missing owner_full_name:        {no_owner}")
        print(f"  Missing owner_mailing_address:  {no_mail}")
        print(f"  Flagged NEEDS_OWNER_LOOKUP:     {needs}")
        print()

        print("First 5 unsent rows (most recently seen):")
        sample = conn.execute(
            """
            SELECT source, case_number, open_date,
                   owner_full_name, owner_mailing_address, comments
              FROM violations
             WHERE lob_letter_id IS NULL
             ORDER BY first_seen_at DESC
             LIMIT 5
            """
        ).fetchall()
        for r in sample:
            print(f"  [{r['source']}/{r['case_number']}]")
            print(f"    open_date:             {r['open_date']}")
            print(f"    owner_full_name:       {(r['owner_full_name'] or '(MISSING)')[:60]}")
            print(f"    owner_mailing_address: {(r['owner_mailing_address'] or '(MISSING)')[:80]}")
            print(f"    comments:              {(r['comments'] or '')[:80]}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
