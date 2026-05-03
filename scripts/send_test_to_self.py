"""
Send N letters to YOURSELF for visual QA before mass-mailing real owners.

Pulls the most recent ready-to-mail rows from the DB, builds the SAME Lob
letter payload that production send.py would build, but:
  - Overrides the TO address with TEST_RECIPIENT_* values from .env so every
    letter lands at YOUR address, not the property owner's.
  - Does NOT update the violations table -- the rows stay "ready to mail" so
    the eventual production run still sends to the real owners.
  - Tags each letter with metadata.test = "true" so they are easy to spot in
    the Lob dashboard.

Run:
    python -m scripts.send_test_to_self --limit 5
    python -m scripts.send_test_to_self --limit 5 --dry-run    # preview only
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from db import init_db
from lob_sender.derive import derive_for_row
from lob_sender.send import (
    fetch_ready_rows,
    _build_letter_payload,
    _post_letter,
    _load_template_html,
)


def get_test_to_address() -> dict:
    """Build the recipient block from TEST_RECIPIENT_* env vars."""
    name  = os.environ.get("TEST_RECIPIENT_NAME", "").strip()
    line1 = os.environ.get("TEST_RECIPIENT_LINE1", "").strip()
    line2 = os.environ.get("TEST_RECIPIENT_LINE2", "").strip()
    city  = os.environ.get("TEST_RECIPIENT_CITY", "").strip()
    state = os.environ.get("TEST_RECIPIENT_STATE", "").strip()
    zipc  = os.environ.get("TEST_RECIPIENT_ZIP", "").strip()

    missing = [k for k, v in {
        "TEST_RECIPIENT_NAME":  name,
        "TEST_RECIPIENT_LINE1": line1,
        "TEST_RECIPIENT_CITY":  city,
        "TEST_RECIPIENT_STATE": state,
        "TEST_RECIPIENT_ZIP":   zipc,
    }.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing required test recipient env vars: "
            + ", ".join(missing)
            + ". See .env.example for the full list."
        )

    return {
        "name":          name[:40],
        "address_line1": line1,
        "address_line2": line2,
        "address_city":  city.upper(),
        "address_state": state.upper(),
        "address_zip":   zipc,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Send test letters to YOURSELF for visual QA.")
    p.add_argument("--limit", type=int, default=3,
                   help="How many letters to send (default: 3, recommended: 3 to 5)")
    p.add_argument("--dry-run", action="store_true",
                   help="Build and preview the Lob payload without calling the API.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    init_db()

    api_key         = os.environ.get("LOB_API_KEY", "").strip()
    template_id     = os.environ.get("LOB_TEMPLATE_ID", "").strip() or None
    from_address_id = os.environ.get("LOB_FROM_ADDRESS_ID", "").strip()

    if not args.dry_run:
        if not api_key:
            print("ERROR: LOB_API_KEY env var required.", file=sys.stderr)
            return 1
        if not from_address_id:
            print("ERROR: LOB_FROM_ADDRESS_ID env var required.", file=sys.stderr)
            return 1
        if not api_key.startswith("live_"):
            print(f"\nWARNING: API key starts with {api_key[:5]!r}, not 'live_'.")
            print("Lob TEST keys do not actually print or mail anything. For visual QA you need a LIVE key.")
            print("Continue anyway (preview-only)? [y/N] ", end="", flush=True)
            if (input() or "n").strip().lower() != "y":
                return 0

    test_to = get_test_to_address()
    print()
    print("Test recipient (every letter goes here):")
    print(f"  {test_to['name']}")
    print(f"  {test_to['address_line1']}")
    if test_to['address_line2']:
        print(f"  {test_to['address_line2']}")
    print(f"  {test_to['address_city']}, {test_to['address_state']} {test_to['address_zip']}")
    print()

    rows = fetch_ready_rows(limit=args.limit)
    if not rows:
        print("No ready-to-mail rows in the DB. Run the connector first.")
        return 0

    template_html = None if template_id else _load_template_html()
    sent = 0
    failed = 0

    for row in rows:
        derived = derive_for_row(dict(row))
        if derived["errors"]:
            print(f"[{row['source']}/{row['case_number']}] SKIP -- {derived['errors']}")
            continue

        # Override the TO address with the test recipient.
        derived["to_address"] = {**test_to}

        payload = _build_letter_payload(
            row=row,
            derived=derived,
            template_id=template_id,
            template_html=template_html,
            from_address_id=from_address_id or "adr_PLACEHOLDER",
            color=os.environ.get("LOB_COLOR", "true").lower() == "true",
            double_sided=os.environ.get("LOB_DOUBLE_SIDED", "true").lower() == "true",
            mail_type=os.environ.get("LOB_MAIL_TYPE", "usps_first_class"),
        )
        # Tag as test so it is obvious in the Lob dashboard.
        payload["description"] = f"TEST / {row['source']} / {row['case_number']}"
        payload.setdefault("metadata", {})["test"] = "true"

        if args.dry_run:
            preview = {**payload, "file": "<template HTML, omitted>"}
            print(json.dumps(preview, indent=2, default=str))
            print("---")
            continue

        # Unique idempotency key per test run so the SAME case can later be
        # sent as a real letter to the real owner without conflicting.
        idem = f"test:{uuid.uuid4()}"
        try:
            resp = _post_letter(payload, api_key=api_key, idempotency_key=idem)
        except Exception as e:
            failed += 1
            print(f"[{row['source']}/{row['case_number']}] FAILED -- {e}")
            continue

        sent += 1
        letter_id = resp.get("id", "")
        mv = derived["merge_variables"]
        print(f"[{row['source']}/{row['case_number']}] sent -> {letter_id}")
        print(f"   first_name={mv.get('first_name')!r}, violation_subject={mv.get('violation_subject')!r}")

    print()
    print(f"Test send summary: {sent} sent, {failed} failed.")
    print()
    print("These letters are tagged metadata.test=\"true\" in Lob. The DB is unchanged,")
    print("so the same cases will still go to the real owners on the next production run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
