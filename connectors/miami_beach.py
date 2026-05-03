"""
Connector: City of Miami Beach -- PRR exports (manual upload).

Drop each new .xlsx into  inbox/miami_beach/  and run this connector
(or click "Process PRR" on the Operator Console).

NOTE: HEADER_ROW and COLUMN_MAP are placeholders. Verify against the first
real export from Miami Beach and adjust if column names differ.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from connectors._manual_upload import process_inbox, default_combine_mailing

SOURCE = "miami_beach"
HEADER_ROW = 0
COLUMN_MAP = {
    "Case Number":           "case_number",
    "Status":                "activity",
    "Date Opened":           "open_date",
    "Violation Type":        "case_type",
    "Property Address":      "property_address",
    "Folio":                 "folio_number",
    "Owner Name":            "owner_full_name",
    "Violation Description": "alleged_violation",
}


def run(inbox_path: Path | None = None) -> dict:
    return process_inbox(
        source=SOURCE,
        header_row=HEADER_ROW,
        column_map=COLUMN_MAP,
        combine_mailing=default_combine_mailing,
        skip_filter=True,
        inbox_path=inbox_path,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=f"Process {SOURCE} inbox files.")
    p.add_argument("--inbox", type=Path)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    summary = run(inbox_path=args.inbox)
    print(f"Files processed: {summary['files_processed']}")
    for r in summary["results"]:
        print(f"  {r}")
