"""
Connector: City of Miami -- PRR Excel ingest.

City of Miami's Code Compliance department fulfills PRRs as an Excel
export from their internal GovOutreach case-management system. The
export carries violation code + description per row (richer than
Palmetto Bay's PDF), and the folio number is embedded in the
"Account Name: Account Name" column as the first 13 digits before
"-ADD-".

Source columns (verified live 2026-06-03 from PRR #26-3395 response):
  - Date/Time Opened           e.g. "1/2/2024, 11:14 AM"
  - Case Number                e.g. "00074659"
  - Violation Address          e.g. "3210 NW 8 AV"
  - Case Violation Name        e.g. "2104 - WORK PERFORMED WITHOUT A FINALIZED PERMIT"
  - Account Name: Account Name e.g. "0131260300530-ADD-131819" (folio = 0131260300530)
  - Commission District        1-5
  - Is Commercial Property     True / False (export is pre-filtered to residential)
  - Case Owner: Full Name      city INSPECTOR, not the property owner
  - Status                     Open | Closed | Lien | Hearing Scheduled | etc.

Scope filter — only ingest rows whose violation code is in the
PSS-trade whitelist. The keyword regex on alleged_violation alone
catches too many tree-work / garage-sale entries that match
"permit" in description without being PSS scope.

Drop each new .xlsx into  inbox/city_of_miami/  and click Process
PRR on the dashboard, OR upload via /upload-prr which dispatches to
this run() automatically.
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from db import init_db, upsert_violations
from parser import clean

SOURCE = "city_of_miami"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INBOX = PROJECT_ROOT / "inbox" / SOURCE

# Map of source columns to canonical violation fields.
COLUMN_MAP = {
    "Date/Time Opened":           "open_date",
    "Case Number":                "case_number",
    "Violation Address":          "property_address",
    "Case Violation Name":        "alleged_violation",
    "Account Name: Account Name": "_folio_raw",   # processed via _extract_folio
    "Commission District":        "district_number",
    "Case Owner: Full Name":      "inspector",
    "Status":                     "activity",
}

# PSS-trade violation code whitelist. Add more codes here as the
# operator identifies them in the queue. Codes are matched against
# the 4-digit prefix of "Case Violation Name" ("2104 - WORK..." → 2104).
SCOPE_CODES: set[str] = {
    "2104",  # WORK PERFORMED WITHOUT A FINALIZED PERMIT — top PSS target
    "7602",  # Working without a permit, building, roofing, mechanical, electrical
    "2144",  # Illegal shed
    "2121",  # Carport, awning and or canopy without permit
}

# Statuses we treat as actively mailable. Closed cases land in the DB
# anyway (for history + dedup) but get auto-flagged do_not_mail so they
# never reach the queue.
ACTIVE_STATUSES = {
    "Open", "Lien", "Hearing Scheduled", "Hearing Completed",
    "Hearing Rescheduled", "Not Appealed", "Guilty", "Pending",
    "Appealed", "Approval Process",
}

_CODE_RE = re.compile(r"^\s*(\d{4})\b")
_FOLIO_RE = re.compile(r"^\s*(\d{13})\b")


def _extract_violation_code(name: str | None) -> str | None:
    if not name:
        return None
    m = _CODE_RE.match(str(name))
    return m.group(1) if m else None


def _extract_folio(account_name: str | None) -> str | None:
    """Pull the leading 13-digit folio out of '0131260300530-ADD-131819'."""
    if not account_name:
        return None
    m = _FOLIO_RE.match(str(account_name))
    return m.group(1) if m else None


def _normalize_datetime(val) -> str | None:
    """Convert '1/2/2024, 11:14 AM' style to ISO YYYY-MM-DD."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        dt = pd.to_datetime(val, errors="coerce")
        if pd.isna(dt) or dt.year < 1900:
            return None
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _build_records(df: pd.DataFrame, *, source_file: str,
                   log: logging.Logger) -> tuple[list[dict], dict]:
    """Build canonical records + return (records, stats) for the operator report."""
    stats = {"raw": len(df), "in_scope": 0, "dropped_off_scope": 0,
             "dropped_commercial": 0, "dropped_missing_case": 0,
             "active": 0, "closed_kept_as_history": 0}
    out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for _, row in df.iterrows():
        # Hard skip: commercial properties (PSS targets homeowners only).
        if row.get("Is Commercial Property") is True:
            stats["dropped_commercial"] += 1
            continue
        case_number = clean(row.get("Case Number"))
        if not case_number:
            stats["dropped_missing_case"] += 1
            continue
        violation_name = clean(row.get("Case Violation Name"))
        code = _extract_violation_code(violation_name)
        if code not in SCOPE_CODES:
            stats["dropped_off_scope"] += 1
            continue
        stats["in_scope"] += 1

        status = clean(row.get("Status")) or "Unknown"
        is_active = status in ACTIVE_STATUSES
        if is_active:
            stats["active"] += 1
        else:
            stats["closed_kept_as_history"] += 1

        rec = {
            # Fields the upsert query expects — keep null for the rest.
            "source":               SOURCE,
            "case_number":          case_number,
            "case_type":            f"code {code}",
            "open_date":            _normalize_datetime(row.get("Date/Time Opened")),
            "close_date":           None,
            "activity_date":        None,
            "activity":             status,
            "inspector":            clean(row.get("Case Owner: Full Name")),
            "deputy_clerk":         None,
            "permit_number":        None,
            "building_code":        None,
            "district_number":      clean(row.get("Commission District")),
            "property_address":     clean(row.get("Violation Address")),
            "folio_number":         _extract_folio(row.get("Account Name: Account Name")),
            "legal_description":    None,
            "owner_full_name":      None,
            "owner_mailing_address": None,
            "violator":             None,
            "alleged_violation":    violation_name,
            "comments":             "NEEDS_OWNER_LOOKUP",
            "matched_keywords":     f"code {code}",
            "first_seen_at":        now,
            "last_seen_at":         now,
            "raw_source_file":      source_file,
            # Carried separately for the post-upsert do_not_mail pass below.
            "_is_active":           is_active,
            "_status":              status,
        }
        out.append(rec)
    return out, stats


def _flag_closed_do_not_mail(records: list[dict], log: logging.Logger) -> int:
    """
    After upsert, mark do_not_mail=1 on rows that came in as Closed (or any
    non-active status). The upsert query doesn't touch do_not_mail directly
    since it's not part of _UPSERT_COLS — so we do a small follow-up.
    """
    import sqlite3
    from db import DB_PATH
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    flagged = 0
    with sqlite3.connect(DB_PATH) as conn:
        for r in records:
            if r["_is_active"]:
                continue
            cur = conn.execute(
                """
                UPDATE violations
                   SET do_not_mail        = 1,
                       do_not_mail_reason = ?,
                       do_not_mail_at     = ?
                 WHERE source = ? AND case_number = ?
                   AND (do_not_mail IS NULL OR do_not_mail = 0)
                """,
                (f"auto: status={r['_status']} at ingest", now,
                 r["source"], r["case_number"]),
            )
            flagged += cur.rowcount
        conn.commit()
    if flagged:
        log.info("auto-marked %d closed-status row(s) do_not_mail", flagged)
    return flagged


def _enrich_owners_from_folios(log: logging.Logger) -> int:
    """Run PA folio lookup on every CoM row still missing owner+mailing."""
    import sqlite3, time
    from db import DB_PATH
    from lookup.property_appraiser import lookup as pa_folio_lookup

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source, case_number, folio_number, comments
              FROM violations
             WHERE source = ?
               AND folio_number IS NOT NULL AND folio_number != ''
               AND ( owner_full_name      IS NULL OR owner_full_name      = ''
                  OR owner_mailing_address IS NULL OR owner_mailing_address = ''
                  OR comments LIKE '%NEEDS_OWNER_LOOKUP%' )
            """,
            (SOURCE,),
        ).fetchall()
    if not rows:
        return 0
    log.info("enriching %d row(s) via Property Appraiser folio lookup", len(rows))
    enriched = 0
    for r in rows:
        try:
            info = pa_folio_lookup(r["folio_number"])
        except Exception as e:
            log.warning("PA lookup error for case=%s folio=%s: %s",
                        r["case_number"], r["folio_number"], e)
            continue
        if not info.found():
            continue
        new_comments = (r["comments"] or "")
        new_comments = " | ".join(
            p.strip() for p in new_comments.split("|")
            if p.strip() and "NEEDS_OWNER_LOOKUP" not in p
        ) or None
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                UPDATE violations
                   SET owner_full_name       = ?,
                       owner_mailing_address = ?,
                       comments              = ?
                 WHERE source = ? AND case_number = ?
                """,
                (info.owner_full_name, info.owner_mailing_address,
                 new_comments, SOURCE, r["case_number"]),
            )
        enriched += 1
        time.sleep(0.25)
    log.info("enriched %d/%d", enriched, len(rows))
    return enriched


def process_file(path: Path, *, log: logging.Logger | None = None) -> dict:
    """Process a single City of Miami PRR Excel into the DB."""
    log = log or logging.getLogger(SOURCE)
    init_db()
    log.info("reading %s", path.name)
    df = pd.read_excel(path, sheet_name=0, header=0)
    # Strip embedded whitespace from column names.
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]
    log.info("loaded %d raw rows", len(df))

    records, stats = _build_records(df, source_file=path.name, log=log)
    log.info("scope filter — kept %d (active=%d, history=%d), "
             "dropped %d off-scope, %d commercial, %d missing case#",
             stats["in_scope"], stats["active"], stats["closed_kept_as_history"],
             stats["dropped_off_scope"], stats["dropped_commercial"],
             stats["dropped_missing_case"])

    # Strip the helper fields before upsert — they aren't DB columns.
    upsertable = [{k: v for k, v in r.items() if not k.startswith("_")}
                  for r in records]
    inserted, updated = upsert_violations(upsertable) if upsertable else (0, 0)
    log.info("DB upsert: %d inserted, %d updated", inserted, updated)

    flagged = _flag_closed_do_not_mail(records, log=log)
    enriched = _enrich_owners_from_folios(log=log)

    # Archive the processed file to keep audit trail.
    archive_dir = PROJECT_ROOT / "inbox" / "processed" / SOURCE
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = archive_dir / f"{stamp}-{path.name}"
    try:
        shutil.copy2(str(path), str(dest))
    except Exception as e:
        log.warning("could not archive %s: %s", path.name, e)

    return {
        "source":           SOURCE,
        "file":             path.name,
        **stats,
        "inserted":         inserted,
        "updated":          updated,
        "closed_flagged":   flagged,
        "enriched":         enriched,
        "archived_to":      str(dest),
    }


def run(inbox_path: Path | None = None) -> dict:
    """Process every .xlsx in inbox/city_of_miami/."""
    log = logging.getLogger(SOURCE)
    init_db()
    inbox = inbox_path or INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    files = sorted([p for p in inbox.glob("*.xls*")
                    if not p.name.startswith("~$")])
    if not files:
        return {"files_processed": 0, "results": []}
    results = []
    for f in files:
        try:
            results.append(process_file(f, log=log))
            # Archive happens inside process_file; clear the inbox after.
            f.unlink()
        except Exception as e:
            log.exception("failed to process %s", f.name)
            results.append({"file": f.name, "error": str(e)})
    return {"files_processed": len(files), "results": results}


def _cli() -> None:
    p = argparse.ArgumentParser(description=f"Process {SOURCE} inbox files.")
    p.add_argument("--inbox", type=Path)
    p.add_argument("--file",  type=Path,
                   help="Process a single file by absolute path (skips the inbox).")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    if args.file:
        summary = process_file(args.file)
    else:
        summary = run(inbox_path=args.inbox)
    print()
    if "results" in summary:
        print(f"Files processed: {summary['files_processed']}")
        for r in summary["results"]:
            print(f"  {r}")
    else:
        for k, v in summary.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    _cli()
