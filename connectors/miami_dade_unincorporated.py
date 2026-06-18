"""
Connector: Miami-Dade Unincorporated — Regulation Cases (Code Enforcement)

Source URL: https://www.miamidade.gov/Apps/RER/RegulationSupportWebViewer/Home/Reports

The Reports page is an ASP.NET form. Submitting it produces a results page with
an "Export to Excel" button that downloads an HTML-disguised .xls file
containing one row per active enforcement case in the chosen window.

This module:
  1. Drives the form via Playwright (handles ASP.NET viewstate automatically)
  2. Captures the export download
  3. Archives the raw file under audit/<source>/<YYYY-MM-DD-HHMM>.xls
  4. Hands it to parser.parse_export() which filters by keyword and normalizes
  5. Upserts into SQLite

Run it manually:
    python -m connectors.miami_dade_unincorporated --start 2026-04-15 --end 2026-04-22

Run it on a schedule (cron / APScheduler) by calling run() with no args — it
defaults to "since the last successful run" (or last 7 days on first run).
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import pandas as pd

from db import init_db, upsert_violations
from parser import build_record

SOURCE = "miami_dade_unincorporated"
REPORTS_URL = "https://www.miamidade.gov/Apps/RER/RegulationSupportWebViewer/Home/Reports"

# Map source-file column name → canonical DB field name
COLUMN_MAP = {
    "CaseNum":          "case_number",          # identity, handled separately
    "CaseType":         "case_type",
    "PropertyAddress":  "property_address",
    "FolioNumber":      "folio_number",
    "CLegal":           "legal_description",
    "OpenDate":         "open_date",
    "CloseDate":        "close_date",
    "DeputyClerk":      "deputy_clerk",
    "Inspector":        "inspector",
    "PermitNum":        "permit_number",
    "FullName":         "owner_full_name",
    "OwnerAdress":      "owner_mailing_address",  # note: misspelled in source
    "BuildingCode":     "building_code",
    "Comments":         "comments",
    "AllegedViolation": "alleged_violation",
    "Violator":         "violator",
    "ActivityDate":     "activity_date",
    "Activity":         "activity",
    "DistrictNumber":   "district_number",
}

import os

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / SOURCE
# STATE_FILE: on Render (or anywhere with a persistent disk), set the DATA_DIR
# env var to that mount (e.g. /var/data) so the last-run date survives deploys.
_DATA_DIR = Path(os.environ.get("DATA_DIR") or (PROJECT_ROOT / "data"))
STATE_FILE = _DATA_DIR / f"{SOURCE}_last_run.txt"

# Form values — match the visible labels exactly
CASE_TYPE = "All Case Types"
TIME_PERIOD_BETWEEN = "Active Cases Opened Between Two Dates"
DISTRICT = "All Districts"

log = logging.getLogger(SOURCE)


def _last_run_date() -> date:
    """Return the date of the last successful run, or 7 days ago on first run."""
    if STATE_FILE.exists():
        try:
            return date.fromisoformat(STATE_FILE.read_text().strip())
        except ValueError:
            pass
    return date.today() - timedelta(days=7)


def _save_last_run(d: date) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(d.isoformat())


def _fmt_mmddyyyy(d: date) -> str:
    """The portal expects MM/DD/YYYY in the date inputs."""
    return d.strftime("%m/%d/%Y")


def fetch_export(start: date, end: date, headless: bool = True) -> Path:
    """
    Drive the Reports form and download the Excel export.
    Returns the path to the archived file.
    """
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    archive_path = AUDIT_DIR / f"{timestamp}_{start.isoformat()}_to_{end.isoformat()}.xls"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.set_default_timeout(30_000)

        log.info("Loading Reports page …")
        page.goto(REPORTS_URL, wait_until="domcontentloaded")

        # The form fields are <select> elements and date inputs. ASP.NET often
        # generates server-rendered IDs, so we select by visible label/role
        # for resilience.
        log.info("Selecting Case Type = %s", CASE_TYPE)
        page.get_by_label("Case Type").select_option(label=CASE_TYPE)

        log.info("Selecting Time Period = %s", TIME_PERIOD_BETWEEN)
        page.get_by_label(TIME_PERIOD_BETWEEN, exact=True).check()

        log.info("Selecting District = %s", DISTRICT)
        page.get_by_label("Commissioner District").select_option(label=DISTRICT)

        log.info("Setting date range %s → %s", start, end)
        page.get_by_label("Start Date").fill(start.strftime("%Y-%m-%d"))
        page.get_by_label("End Date").fill(end.strftime("%Y-%m-%d"))

        log.info("Submitting form …")
        page.get_by_role("button", name="Submit").click()

        # Wait for the results page to render. The export button only exists
        # on the results page.
        page.wait_for_load_state("networkidle", timeout=60_000)

        log.info("Triggering Export to Excel download …")
        with page.expect_download(timeout=60_000) as dl_info:
            # On the results page this control is rendered as an <a> link,
            # not a <button>. get_by_role("link", ...) matches either.
            page.get_by_role("link", name="Export to Excel").click()
        download = dl_info.value

        download.save_as(archive_path)
        log.info("Saved raw export → %s", archive_path)

        browser.close()

    return archive_path


def parse_export(path: Path) -> list[dict]:
    """
    Parse a Miami-Dade Reports export (HTML disguised as .xls) into normalized
    canonical records. Applies the global keyword filter.
    """
    tables = pd.read_html(path)
    if not tables:
        return []
    df = tables[0]

    out: list[dict] = []
    for _, row in df.iterrows():
        case_number = row.get("CaseNum")
        fields = {
            canonical: row.get(src_col)
            for src_col, canonical in COLUMN_MAP.items()
            if canonical != "case_number"
        }
        rec = build_record(
            source=SOURCE,
            case_number=case_number,
            fields=fields,
            raw_source_file=str(path),
        )
        if rec:
            out.append(rec)
    return out


def run(start: date | None = None, end: date | None = None, headless: bool = True) -> dict:
    """
    Full connector run: fetch → parse → filter → upsert.
    Returns a summary dict for logging / dashboard.
    """
    init_db()

    end = end or date.today()
    start = start or _last_run_date()
    if start > end:
        start = end - timedelta(days=1)

    log.info("=== %s run: %s → %s ===", SOURCE, start, end)

    try:
        archive_path = fetch_export(start, end, headless=headless)
    except PWTimeout as e:
        log.error("Timed out talking to portal: %s", e)
        raise

    log.info("Parsing and filtering export …")
    rows = parse_export(archive_path)
    log.info("Matched %d rows after keyword filter", len(rows))

    inserted, updated = upsert_violations(rows)
    log.info("DB upsert: %d inserted, %d updated", inserted, updated)

    _save_last_run(end)

    return {
        "source": SOURCE,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "matched": len(rows),
        "inserted": inserted,
        "updated": updated,
        "archive": str(archive_path),
    }


CASE_DETAILS_URL = (
    "https://www.miamidade.gov/Apps/RER/RegulationSupportWebViewer/Home/CaseDetails?caseNum={case}"
)
_LOOKUP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# CaseDetails page label -> canonical field. (Owner Adress is misspelled in the
# county's markup, same as the Excel export.)
_DETAIL_LABELS = {
    "Case Number":       "case_number",
    "Case Type":         "case_type",
    "Property Address":  "property_address",
    "Folio Number":      "folio_number",
    "Legal Description": "legal_description",
    "Open Date":         "open_date",
    "Close Date":        "close_date",
    "DeputyClerk":       "deputy_clerk",
    "Inspector":         "inspector",
    "Permit Number":     "permit_number",
    "Owner Name":        "owner_full_name",
    "Owner Adress":      "owner_mailing_address",
    "Building Code":     "building_code",
    "Comments":          "comments",
    "AllegedViolation":  "alleged_violation",
}


def lookup_case(case_number: str) -> dict | None:
    """Live single-case lookup against the county's CaseDetails page.

    Returns a dict of canonical fields, or None if the case isn't found or the
    request fails. Read-only — does not touch the DB. Fast (short timeout) and
    swallows transport errors, for use by the universal search bar."""
    import requests
    from lxml import html as lxml_html

    case_number = (case_number or "").strip()
    if not case_number:
        return None
    try:
        r = requests.get(
            CASE_DETAILS_URL.format(case=case_number),
            headers={"User-Agent": _LOOKUP_UA}, timeout=12,
        )
        r.raise_for_status()
    except requests.RequestException:
        return None

    doc = lxml_html.fromstring(r.text)
    out: dict = {}
    for tr in doc.xpath("//tr"):
        cells = [c.text_content().strip() for c in tr.xpath("./td|./th")]
        if len(cells) >= 2 and cells[0].rstrip(":").strip() in _DETAIL_LABELS:
            out[_DETAIL_LABELS[cells[0].rstrip(":").strip()]] = cells[1].strip()

    # Guard: a missing case returns a generic page with no Case Number row, and
    # we want the returned number to actually match what we asked for.
    if not out.get("case_number") or out["case_number"] != case_number:
        return None
    return out


def import_case(case_number: str) -> dict | None:
    """Live-lookup a case and upsert it into the violations table, bypassing the
    keyword filter (the operator chose to pull it, so scope doesn't gate it).
    Returns the upserted record, or None if the case couldn't be found."""
    fields = lookup_case(case_number)
    if not fields:
        return None
    init_db()
    record = build_record(
        source=SOURCE,
        case_number=fields["case_number"],
        fields={k: v for k, v in fields.items() if k != "case_number"},
        raw_source_file="live:CaseDetails",
        skip_filter=True,
    )
    if record:
        upsert_violations([record])
    return record


def _cli() -> None:
    parser = argparse.ArgumentParser(description=f"Run the {SOURCE} connector.")
    parser.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD (default: last run date or 7 days ago)")
    parser.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--show-browser", action="store_true", help="Run with a visible browser (debugging)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    summary = run(start=args.start, end=args.end, headless=not args.show_browser)
    print()
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _cli()
