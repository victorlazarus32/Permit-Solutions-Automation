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
import base64
import json
import logging
import os
import sys
import urllib.error
import urllib.request
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
    LOB_API_BASE,
    TEMPLATE_PATH,
    fetch_ready_rows,
    _basic_auth_header,
    _build_letter_payload,
    _post_letter,
    _load_template_html,
)


def _lob_get(path: str, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{LOB_API_BASE}{path}",
        method="GET",
        headers={"Authorization": _basic_auth_header(api_key)},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _lob_post(path: str, payload: dict, api_key: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LOB_API_BASE}{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": _basic_auth_header(api_key),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lob API error {e.code} on POST {path}: {err_body}") from e


def _env_path() -> Path:
    return PROJECT_ROOT / ".env"


def _write_back_env(key: str, value: str) -> None:
    """Replace `KEY=...` (anywhere in .env) with the new value. Appends if missing."""
    env_path = _env_path()
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def _bootstrap_test_from_address(test_api_key: str, live_api_key: str, live_from_id: str) -> str:
    """Mirror the live return address under test mode and persist the ID."""
    if not live_from_id:
        raise RuntimeError("LOB_FROM_ADDRESS_ID (live) must be set so the test mirror can match.")
    print(f"Fetching live from-address {live_from_id} to mirror under test mode …")
    live = _lob_get(f"/addresses/{live_from_id}", api_key=live_api_key)
    test_payload = {
        "description": (live.get("description") or "Permit Solutions return address") + " (TEST)",
        "name":           live.get("name") or "",
        "company":        live.get("company") or "",
        "address_line1":  live.get("address_line1") or "",
        "address_line2":  live.get("address_line2") or "",
        "address_city":   live.get("address_city") or "",
        "address_state":  live.get("address_state") or "",
        "address_zip":    live.get("address_zip") or "",
    }
    # Strip empty optional fields so Lob doesn't reject blanks. Country is
    # omitted on purpose -- Lob's GET returns the long-form ("UNITED STATES")
    # but POST /addresses requires the ISO-3166 code ("US"), and Lob defaults
    # to US when omitted, which is what we want for every mailpiece anyway.
    test_payload = {k: v for k, v in test_payload.items() if v != ""}
    resp = _lob_post("/addresses", test_payload, api_key=test_api_key)
    adr_id = resp.get("id", "")
    if not adr_id:
        raise RuntimeError(f"Test address creation returned no id: {resp}")
    _write_back_env("LOB_TEST_FROM_ADDRESS_ID", adr_id)
    print(f"   created test from-address: {adr_id} (saved to .env)")
    return adr_id


def _bootstrap_test_template(test_api_key: str) -> str:
    """Upload the letter template under test mode and persist the ID."""
    print(f"Uploading {TEMPLATE_PATH.name} to Lob test mode …")
    payload = {
        "description": "Permit Solutions — Violation Notice (TEST)",
        "html":        TEMPLATE_PATH.read_text(encoding="utf-8"),
        "engine":      "legacy",
    }
    resp = _lob_post("/templates", payload, api_key=test_api_key)
    tmpl_id = resp.get("id", "")
    if not tmpl_id:
        raise RuntimeError(f"Test template upload returned no id: {resp}")
    _write_back_env("LOB_TEST_TEMPLATE_ID", tmpl_id)
    print(f"   created test template:    {tmpl_id} (saved to .env)")
    return tmpl_id


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
    p.add_argument("--source", default=None,
                   help="Restrict to one source (e.g. 'homestead', 'miami_dade_unincorporated').")
    p.add_argument("--dry-run", action="store_true",
                   help="Build and preview the Lob payload without calling the API.")
    p.add_argument("--test-api", action="store_true",
                   help="Send through LOB_TEST_API_KEY instead of the live key. Lob renders "
                        "a PDF in the dashboard but nothing prints or mails. Bootstraps "
                        "LOB_TEST_FROM_ADDRESS_ID + LOB_TEST_TEMPLATE_ID on first run.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    init_db()

    if args.test_api:
        api_key         = os.environ.get("LOB_TEST_API_KEY", "").strip()
        live_api_key    = os.environ.get("LOB_API_KEY", "").strip()
        live_from_id    = os.environ.get("LOB_FROM_ADDRESS_ID", "").strip()
        from_address_id = os.environ.get("LOB_TEST_FROM_ADDRESS_ID", "").strip()
        template_id     = os.environ.get("LOB_TEST_TEMPLATE_ID", "").strip() or None

        if not args.dry_run:
            if not api_key:
                print("ERROR: LOB_TEST_API_KEY env var required for --test-api.", file=sys.stderr)
                return 1
            if not api_key.startswith("test_"):
                print(f"ERROR: LOB_TEST_API_KEY should start with 'test_', got {api_key[:5]!r}.", file=sys.stderr)
                return 1
            if not from_address_id:
                from_address_id = _bootstrap_test_from_address(api_key, live_api_key, live_from_id)
            if not template_id:
                template_id = _bootstrap_test_template(api_key)
    else:
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
                print("Lob TEST keys do not actually print or mail anything. For visual QA you need a LIVE key,")
                print("or re-run with --test-api to send through LOB_TEST_API_KEY explicitly.")
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

    rows = fetch_ready_rows(limit=args.limit, source=args.source)
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
            address_placement=os.environ.get("LOB_ADDRESS_PLACEMENT", "insert_blank_page"),
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
