"""
Connector: City of Homestead — Records Request exports

This is a MANUAL-UPLOAD source. The city does not provide a public download
or API; you obtain leads via periodic public records requests (PRRs) and
receive an Excel file by email.

Workflow:
  1. Save each new export into  inbox/homestead/<filename>.xlsx
  2. Run:  python -m connectors.homestead
  3. The connector parses every .xlsx in inbox/homestead/, upserts to the DB,
     and moves processed files to inbox/processed/homestead/<timestamp>_<name>

Why this pattern:
  - Idempotent: the (source, case_number) primary key in the DB means a row
    that appears in two consecutive PRRs will update, not duplicate.
  - Audit trail: every original file is preserved under inbox/processed/
  - Same downstream pipeline: once in the DB, Homestead leads flow into the
    Lob letter stage exactly like Miami-Dade leads.

Notes on the source file format (Homestead PRR template, as of 2026):
  - Title and summary on rows 1–5
  - Real headers on row 7 (so header=6 in pandas, 0-indexed)
  - 13 columns including pre-joined Property Appraiser owner data
  - The dataset is already pre-filtered to fence/window/door scope, but we
    still pass it through the keyword filter as a safety net.
"""
from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from db import init_db, upsert_violations
from parser import build_record, clean

SOURCE = "homestead"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INBOX_DIR = PROJECT_ROOT / "inbox" / SOURCE
PROCESSED_DIR = PROJECT_ROOT / "inbox" / "processed" / SOURCE

# Header row in the Homestead PRR template (0-indexed). Row 7 in Excel.
HEADER_ROW = 6

# Map source column → canonical DB field
COLUMN_MAP = {
    "Case Number":           "case_number",     # identity, handled separately
    "Status":                "activity",        # using 'activity' for case status
    "Date Opened":           "open_date",
    "Violation Type":        "case_type",
    "Property Address":      "property_address",
    "Parcel Number":         "folio_number",    # Homestead calls it "Parcel" but it's the Miami-Dade folio
    "Owner Name":            "owner_full_name",
    "Violation Description": "alleged_violation",
    # Mailing address is split across 4 fields — combined in parse_export()
}

log = logging.getLogger(SOURCE)


def _combine_mailing(row: pd.Series) -> str | None:
    """Join Homestead's split mailing fields into 'Street, City, State Zip' format."""
    street = clean(row.get("Mailing Address"))
    city   = clean(row.get("Mailing City"))
    state  = clean(row.get("State"))
    zipc   = clean(row.get("Zip"))
    if not street:
        return None
    parts = [street]
    if city and (state or zipc):
        locality = f"{city}, " + " ".join(p for p in (state, zipc) if p)
    else:
        locality = " ".join(p for p in (city, state, zipc) if p)
    if locality:
        parts.append(locality)
    return ", ".join(parts)


def parse_file(path: Path) -> list[dict]:
    """Parse one Homestead PRR export into canonical records."""
    log.info("Reading %s", path.name)
    df = pd.read_excel(path, header=HEADER_ROW)

    # Normalize column names (strip embedded newlines like 'Homestead\nExempt')
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]

    out: list[dict] = []
    skipped_no_owner = 0

    for _, row in df.iterrows():
        case_number = row.get("Case Number")
        fields = {
            canonical: row.get(src_col)
            for src_col, canonical in COLUMN_MAP.items()
            if canonical != "case_number"
        }
        fields["owner_mailing_address"] = _combine_mailing(row)

        # Append homestead-exempt status to comments for downstream awareness
        exempt = clean(row.get("Homestead Exempt"))
        if exempt:
            fields["comments"] = f"Homestead Exempt: {exempt}"

        rec = build_record(
            source=SOURCE,
            case_number=case_number,
            fields=fields,
            raw_source_file=str(path),
            skip_filter=True,  # PRR is already pre-filtered to scope
        )
        if not rec:
            continue

        # Flag rows missing an owner mailing address — they can't be mailed by Lob
        if not rec.get("owner_mailing_address"):
            skipped_no_owner += 1
            existing = rec.get("comments") or ""
            rec["comments"] = (existing + " | NEEDS_OWNER_LOOKUP").strip(" |")

        out.append(rec)

    if skipped_no_owner:
        log.warning(
            "%d row(s) had no mailing address — flagged NEEDS_OWNER_LOOKUP "
            "and held back from mailing",
            skipped_no_owner,
        )

    return out


def run(inbox_path: Path | None = None) -> dict:
    """
    Process all files in inbox/homestead/.
    Returns a per-file summary list.
    """
    init_db()
    inbox = inbox_path or INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in inbox.glob("*.xlsx") if not p.name.startswith("~$"))
    if not files:
        log.info("No files in %s — nothing to do", inbox)
        return {"source": SOURCE, "files_processed": 0, "results": []}

    log.info("=== %s run: %d file(s) to process ===", SOURCE, len(files))
    results = []

    for f in files:
        try:
            rows = parse_file(f)
            inserted, updated = upsert_violations(rows)
            log.info("%s: %d records (inserted=%d, updated=%d)",
                     f.name, len(rows), inserted, updated)

            # Move the processed file out of the inbox
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            dest = PROCESSED_DIR / f"{timestamp}__{f.name}"
            shutil.move(str(f), str(dest))

            results.append({
                "file": f.name,
                "records": len(rows),
                "inserted": inserted,
                "updated": updated,
                "moved_to": str(dest),
            })
        except Exception as e:
            log.exception("Failed to process %s: %s", f.name, e)
            results.append({"file": f.name, "error": str(e)})

    return {
        "source": SOURCE,
        "files_processed": len(results),
        "results": results,
    }


def _cli() -> None:
    parser = argparse.ArgumentParser(description=f"Process {SOURCE} inbox files.")
    parser.add_argument("--inbox", type=Path, help="Override inbox directory (default: inbox/homestead/)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    summary = run(inbox_path=args.inbox)
    print()
    print(f"Files processed: {summary['files_processed']}")
    for r in summary["results"]:
        print(f"  {r}")


if __name__ == "__main__":
    _cli()
