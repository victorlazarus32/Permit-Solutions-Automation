"""
Lob letter sender.

Reads rows from the `violations` DB that are ready to mail (have a parseable
mailing address, haven't been sent yet, not flagged for owner lookup), submits
each to the Lob Letters API, and writes the resulting letter ID + status back
to the DB.

Why no SDK: Lob's API is small enough that direct HTTPS via urllib avoids a
dependency. Switch to the official `lob` package later if you want.

Required env vars:
  LOB_API_KEY         - Your Lob secret key (test_* or live_*).
  LOB_TEMPLATE_ID     - Template ID returned by Lob when you upload
                        templates/violation_letter_en.html (e.g. tmpl_xxx).
                        While developing, leave unset and the inline HTML
                        will be sent each time (slower, but no upload step).
  LOB_FROM_ADDRESS_ID - Address ID for your return address (adr_xxx).
                        Required by Lob — every letter needs a `from`.

Optional env vars:
  LOB_COLOR=true|false       - Color print (default: true)
  LOB_DOUBLE_SIDED=true|false - Print both sides (default: false; we use 1 page)
  LOB_MAIL_TYPE              - usps_first_class (default) or usps_standard
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db import connect, init_db
from lob_sender.derive import derive_for_row

LOB_API_BASE = "https://api.lob.com/v1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "violation_letter_en.html"

log = logging.getLogger("lob_sender")


# ---------- Low-level HTTP ----------

def _basic_auth_header(api_key: str) -> str:
    """Lob: API key as username, blank password."""
    raw = f"{api_key}:".encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post_letter(payload: dict, api_key: str, idempotency_key: str) -> dict:
    """POST /v1/letters. Raises on HTTP errors with the Lob error body included."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LOB_API_BASE}/letters",
        data=body,
        method="POST",
        headers={
            "Authorization": _basic_auth_header(api_key),
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "permit-solutions/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = "<no body>"
        raise RuntimeError(
            f"Lob API error {e.code}: {err_body}"
        ) from e


# ---------- DB helpers ----------

READY_TO_MAIL_SQL = """
    SELECT *
    FROM violations
    WHERE owner_mailing_address IS NOT NULL
      AND owner_full_name      IS NOT NULL
      AND lob_letter_id        IS NULL
      AND (comments NOT LIKE '%NEEDS_OWNER_LOOKUP%' OR comments IS NULL)
    ORDER BY first_seen_at ASC
"""


def fetch_ready_rows(limit: int | None = None) -> list[sqlite3.Row]:
    """Return rows that are eligible for mailing."""
    sql = READY_TO_MAIL_SQL
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return list(conn.execute(sql))


def update_lob_state(
    *,
    source: str,
    case_number: str,
    letter_id: str,
    status: str,
    mailed_at: str | None = None,
) -> None:
    """Write back the letter ID and status. Called once per successful send."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            UPDATE violations
            SET lob_letter_id     = ?,
                lob_status        = ?,
                lob_mailed_at     = COALESCE(?, lob_mailed_at),
                lob_last_event_at = ?
            WHERE source = ? AND case_number = ?
            """,
            (letter_id, status, mailed_at, now, source, case_number),
        )


# ---------- Build the Lob payload ----------

def _load_template_html() -> str:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Letter template missing at {TEMPLATE_PATH}")
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _build_letter_payload(
    *,
    row: sqlite3.Row,
    derived: dict,
    template_id: str | None,
    template_html: str | None,
    from_address_id: str,
    color: bool,
    double_sided: bool,
    mail_type: str,
) -> dict:
    """Construct the Lob /v1/letters request body."""
    payload: dict[str, Any] = {
        "description": f"{row['source']} / {row['case_number']}",
        "to":   derived["to_address"],
        "from": from_address_id,
        # `file` is the HTML or a template ID. Lob accepts either.
        "file": template_id if template_id else (template_html or _load_template_html()),
        "merge_variables": derived["merge_variables"],
        "color":            color,
        "double_sided":     double_sided,
        "mail_type":        mail_type,
        "use_type":         "operational",   # not marketing — these are notices
        "metadata": {
            # Lob caps each metadata value at 500 chars; cap source/case to be safe
            "source":      str(row["source"])[:40],
            "case_number": str(row["case_number"])[:40],
            "folio":       (row["folio_number"] or "")[:40],
        },
    }
    return payload


# ---------- Main loop ----------

def send_batch(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Process up to `limit` ready rows.
    If dry_run=True, builds the payload and prints it but does not call Lob.
    """
    init_db()

    api_key         = os.environ.get("LOB_API_KEY", "").strip()
    template_id     = os.environ.get("LOB_TEMPLATE_ID", "").strip() or None
    from_address_id = os.environ.get("LOB_FROM_ADDRESS_ID", "").strip()
    color           = os.environ.get("LOB_COLOR", "true").lower() == "true"
    double_sided    = os.environ.get("LOB_DOUBLE_SIDED", "false").lower() == "true"
    mail_type       = os.environ.get("LOB_MAIL_TYPE", "usps_first_class")

    if not dry_run:
        if not api_key:
            raise RuntimeError("LOB_API_KEY env var is required (use --dry-run to preview without it).")
        if not from_address_id:
            raise RuntimeError("LOB_FROM_ADDRESS_ID env var is required — create your return address in the Lob dashboard first.")

    template_html = None if template_id else _load_template_html()

    rows = fetch_ready_rows(limit=limit)
    log.info("%d row(s) eligible for mailing (limit=%s)", len(rows), limit)

    sent = 0
    skipped = 0
    failed  = 0
    results: list[dict] = []

    for row in rows:
        derived = derive_for_row(dict(row))
        if derived["errors"]:
            skipped += 1
            log.warning(
                "[%s/%s] SKIP — %s",
                row["source"], row["case_number"], ",".join(derived["errors"]),
            )
            results.append({
                "source": row["source"],
                "case_number": row["case_number"],
                "status": "skipped",
                "errors": derived["errors"],
            })
            continue

        payload = _build_letter_payload(
            row=row,
            derived=derived,
            template_id=template_id,
            template_html=template_html,
            from_address_id=from_address_id or "adr_PLACEHOLDER",
            color=color,
            double_sided=double_sided,
            mail_type=mail_type,
        )

        if dry_run:
            preview = {**payload, "file": "<template HTML, omitted>"}
            print(json.dumps(preview, indent=2, default=str))
            print("---")
            results.append({
                "source": row["source"],
                "case_number": row["case_number"],
                "status": "dry_run",
            })
            continue

        # Idempotency: deterministic key per (source, case_number) means a retry
        # within 24 hours produces the SAME letter, not a duplicate.
        idem = f"{row['source']}:{row['case_number']}:{uuid.uuid5(uuid.NAMESPACE_DNS, row['source']+row['case_number'])}"
        try:
            resp = _post_letter(payload, api_key=api_key, idempotency_key=idem)
        except Exception as e:
            failed += 1
            log.error("[%s/%s] FAILED — %s", row["source"], row["case_number"], e)
            results.append({
                "source": row["source"],
                "case_number": row["case_number"],
                "status": "failed",
                "error": str(e)[:500],
            })
            continue

        letter_id = resp.get("id", "")
        status = resp.get("status", "created") or "created"
        send_date = resp.get("send_date") or resp.get("date_created")
        update_lob_state(
            source=row["source"],
            case_number=row["case_number"],
            letter_id=letter_id,
            status=status,
            mailed_at=send_date,
        )
        sent += 1
        log.info("[%s/%s] sent → %s (%s)", row["source"], row["case_number"], letter_id, status)
        results.append({
            "source": row["source"],
            "case_number": row["case_number"],
            "status": "sent",
            "letter_id": letter_id,
        })

    return {
        "considered": len(rows),
        "sent":       sent,
        "skipped":    skipped,
        "failed":     failed,
        "dry_run":    dry_run,
        "results":    results,
    }


def _cli() -> None:
    p = argparse.ArgumentParser(description="Send queued violation letters via Lob.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of letters this run (default: send all eligible).")
    p.add_argument("--dry-run", action="store_true",
                   help="Build payloads and print them, but do not call Lob.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    summary = send_batch(limit=args.limit, dry_run=args.dry_run)
    print()
    print(f"Considered: {summary['considered']}")
    print(f"  Sent:    {summary['sent']}")
    print(f"  Skipped: {summary['skipped']}")
    print(f"  Failed:  {summary['failed']}")


if __name__ == "__main__":
    _cli()
