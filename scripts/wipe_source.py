"""
Wipe all violation rows for a given source from the database.

Useful for cleaning out a scraper's bad cold-start results (e.g., when the
Tyler EnerGov connector pulls the oldest 300 cases instead of recent ones)
so the next run can re-populate with current data.

Usage:
    python -m scripts.wipe_source homestead
    python -m scripts.wipe_source miami_dade_unincorporated
"""
from __future__ import annotations

import sqlite3
import sys

from db import DB_PATH


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.wipe_source <source_key>", file=sys.stderr)
        print("Example: python -m scripts.wipe_source homestead", file=sys.stderr)
        return 1
    source = sys.argv[1].strip()
    if not source:
        print("Source key is required.", file=sys.stderr)
        return 1
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM violations WHERE source = ?", (source,)
        )
        n = cur.rowcount
    print(f"Deleted {n} rows for source={source!r} from {DB_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
