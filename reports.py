"""
Daily/range mailing reports.

A "sent letter" is any violation row with `lob_letter_id IS NOT NULL` — Lob
accepted and returned a letter ID. We don't currently distinguish "accepted
by Lob" from "actually deposited at USPS" (that's a future webhook job).

When we ask "when was this letter sent?" we use `lob_mailed_at` if Lob has
told us, otherwise fall back to `last_seen_at` (the row's last touch, which
on a successful send is approximately the send time).
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

from db import DB_PATH


# When the row's lob_mailed_at hasn't been populated yet, fall back to
# last_seen_at as a proxy. COALESCE picks the first non-null.
_SENT_AT_EXPR = "COALESCE(lob_mailed_at, last_seen_at)"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def total_sent(since: str | None = None, until: str | None = None) -> int:
    """Count letters sent in the date window (inclusive both ends)."""
    sql, params = _base_sent_query(since, until)
    with _conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]


def by_source(since: str | None = None, until: str | None = None) -> list[dict]:
    """[{source, count}, ...] sorted by count desc."""
    sql, params = _base_sent_query(since, until)
    with _conn() as c:
        rows = c.execute(
            f"SELECT source, COUNT(*) AS n FROM ({sql}) GROUP BY source ORDER BY n DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def by_keyword(since: str | None = None, until: str | None = None) -> list[dict]:
    """
    [{keyword, count}, ...] sorted by count desc.

    matched_keywords is a comma-separated string per row. A row with
    "fence,pergola" contributes 1 to fence AND 1 to pergola.
    """
    sql, params = _base_sent_query(since, until, columns="matched_keywords")
    counter: Counter[str] = Counter()
    with _conn() as c:
        for row in c.execute(sql, params):
            kws = (row["matched_keywords"] or "").strip()
            if not kws:
                continue
            for k in (p.strip().lower() for p in kws.split(",")):
                if k:
                    counter[k] += 1
    return [{"keyword": k, "count": n} for k, n in counter.most_common()]


def by_source_and_keyword(since: str | None = None,
                          until: str | None = None) -> dict:
    """
    Cross-tab: { keyword: {source: count, ...}, ... } plus row/col totals.

    Returns:
        {
            "keywords": [str, ...]    # ordered desc by total
            "sources":  [str, ...]    # ordered desc by total
            "matrix":   { keyword: { source: count } }
            "row_totals": { keyword: total }
            "col_totals": { source: total }
            "grand_total": int
        }
    """
    sql, params = _base_sent_query(since, until, columns="source, matched_keywords")
    matrix: dict[str, Counter[str]] = {}
    col_totals: Counter[str] = Counter()
    grand_total = 0

    with _conn() as c:
        for row in c.execute(sql, params):
            src = row["source"]
            kws = [k.strip().lower() for k in (row["matched_keywords"] or "").split(",")
                   if k.strip()]
            col_totals[src] += 1
            grand_total += 1
            for k in (kws or ["(unmatched)"]):
                matrix.setdefault(k, Counter())[src] += 1

    row_totals = {k: sum(v.values()) for k, v in matrix.items()}
    keywords = sorted(matrix.keys(), key=lambda k: (-row_totals[k], k))
    sources = sorted(col_totals.keys(), key=lambda s: (-col_totals[s], s))

    return {
        "keywords":    keywords,
        "sources":     sources,
        "matrix":      {k: dict(v) for k, v in matrix.items()},
        "row_totals":  row_totals,
        "col_totals":  dict(col_totals),
        "grand_total": grand_total,
    }


def per_day(since: str | None = None, until: str | None = None) -> list[dict]:
    """
    [{day: 'YYYY-MM-DD', count: int}, ...] sorted oldest first.

    Useful for a small bar chart / day-by-day rundown.
    """
    sql, params = _base_sent_query(since, until, columns=f"date({_SENT_AT_EXPR}) AS day")
    with _conn() as c:
        rows = c.execute(
            f"SELECT day, COUNT(*) AS n FROM ({sql}) GROUP BY day ORDER BY day ASC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def recent_letters(since: str | None = None, until: str | None = None,
                   limit: int = 50) -> list[dict]:
    """The N most recent sent letters with one-line detail."""
    sql, params = _base_sent_query(
        since, until,
        columns=f"source, case_number, owner_full_name, property_address, "
                f"matched_keywords, lob_letter_id, {_SENT_AT_EXPR} AS sent_at",
    )
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM ({sql}) ORDER BY sent_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- internal ----------

def _base_sent_query(since: str | None, until: str | None,
                     columns: str = "*") -> tuple[str, list]:
    """
    Build the inner SELECT that filters violations to "sent in date window".
    Returns (sql, params). The caller wraps this in COUNT/GROUP BY/etc.
    """
    sql = f"""
        SELECT {columns}
          FROM violations
         WHERE lob_letter_id IS NOT NULL AND lob_letter_id <> ''
    """
    params: list = []
    if since:
        sql += f" AND date({_SENT_AT_EXPR}) >= ?"
        params.append(since)
    if until:
        sql += f" AND date({_SENT_AT_EXPR}) <= ?"
        params.append(until)
    return sql, params


def default_window(days: int = 30) -> tuple[str, str]:
    """Convenience: (since_iso, until_iso) for the last N days inclusive."""
    today = date.today()
    return ((today - timedelta(days=days - 1)).isoformat(), today.isoformat())


def recent_daily_runs(limit: int = 14) -> list[dict]:
    """Most recent daily_runs rows for the Reports page."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM daily_runs ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]
