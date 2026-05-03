"""
Shared base for manual-upload (PRR) city connectors.

Each city's PRR Excel export has its own column names and structure, but the
processing flow is identical: read every file in the inbox, normalize columns,
upsert into the DB, archive the file.

A per-city connector (e.g. connectors/palmetto_bay.py) defines:
  - SOURCE         (canonical DB key, lowercase snake_case)
  - HEADER_ROW     (0-indexed row that contains the column headers)
  - COLUMN_MAP     (dict: source-file column name -> canonical DB field name)
  - COMBINE_MAILING (optional callable(row) -> mailing-address string)
  - SKIP_FILTER    (default True; PRR exports are typically pre-scoped)

...then calls process_inbox(...) below.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from db import init_db, upsert_violations
from parser import build_record, clean

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_file(
    path: Path,
    *,
    source: str,
    header_row: int,
    column_map: dict,
    combine_mailing: Callable | None,
    skip_filter: bool,
    log: logging.Logger,
) -> list[dict]:
    """Parse one PRR Excel file into a list of canonical record dicts."""
    log.info("Reading %s", path.name)
    df = pd.read_excel(path, header=header_row)

    # Strip embedded newlines and surrounding whitespace from header names
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]

    out: list[dict] = []
    skipped_no_owner = 0

    for _, row in df.iterrows():
        case_number = row.get(_invert_lookup(column_map, "case_number"))
        fields = {
            canonical: row.get(src_col)
            for src_col, canonical in column_map.items()
            if canonical != "case_number"
        }
        if combine_mailing is not None:
            mailing = combine_mailing(row)
            if mailing:
                fields["owner_mailing_address"] = mailing

        rec = build_record(
            source=source,
            case_number=case_number,
            fields=fields,
            raw_source_file=str(path),
            skip_filter=skip_filter,
        )
        if not rec:
            continue

        if not rec.get("owner_mailing_address"):
            skipped_no_owner += 1
            existing = rec.get("comments") or ""
            rec["comments"] = (existing + " | NEEDS_OWNER_LOOKUP").strip(" |")

        out.append(rec)

    if skipped_no_owner:
        log.warning(
            "%d row(s) had no mailing address -- flagged NEEDS_OWNER_LOOKUP "
            "and held back from mailing", skipped_no_owner,
        )

    return out


def _invert_lookup(column_map: dict, target: str) -> str | None:
    """Return the source-file column whose canonical mapping is `target`."""
    for k, v in column_map.items():
        if v == target:
            return k
    return None


def process_inbox(
    *,
    source: str,
    header_row: int,
    column_map: dict,
    combine_mailing: Callable | None = None,
    skip_filter: bool = True,
    inbox_path: Path | None = None,
) -> dict:
    """Process every .xlsx in inbox/<source>/, upsert, then archive each file."""
    log = logging.getLogger(source)
    init_db()
    inbox     = inbox_path or PROJECT_ROOT / "inbox" / source
    processed = PROJECT_ROOT / "inbox" / "processed" / source
    inbox.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in inbox.glob("*.xlsx") if not p.name.startswith("~$"))
    if not files:
        log.info("No files in %s -- nothing to do", inbox)
        return {"source": source, "files_processed": 0, "results": []}

    log.info("=== %s run: %d file(s) to process ===", source, len(files))
    results = []

    for f in files:
        try:
            rows = parse_file(
                f,
                source=source,
                header_row=header_row,
                column_map=column_map,
                combine_mailing=combine_mailing,
                skip_filter=skip_filter,
                log=log,
            )
            inserted, updated = upsert_violations(rows)
            log.info("%s: %d records (inserted=%d, updated=%d)",
                     f.name, len(rows), inserted, updated)

            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            dest = processed / f"{timestamp}__{f.name}"
            shutil.move(str(f), str(dest))

            results.append({
                "file":     f.name,
                "records":  len(rows),
                "inserted": inserted,
                "updated":  updated,
                "moved_to": str(dest),
            })
        except Exception as e:
            log.exception("Failed to process %s: %s", f.name, e)
            results.append({"file": f.name, "error": str(e)})

    return {
        "source":          source,
        "files_processed": len(results),
        "results":         results,
    }


# A reasonable default mailing-address combiner for cities that split mailing
# into Street / City / State / Zip columns. Override per city if needed.
def default_combine_mailing(row, *,
                            street_col: str = "Mailing Address",
                            city_col:   str = "Mailing City",
                            state_col:  str = "State",
                            zip_col:    str = "Zip") -> str | None:
    street = clean(row.get(street_col))
    city   = clean(row.get(city_col))
    state  = clean(row.get(state_col))
    zipc   = clean(row.get(zip_col))
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
