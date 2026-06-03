"""
Connector: ASP Government eTRAKiT — public code-case search.

Generic, tenant-aware. Each eTRAKiT-hosted city gets a config block in the
ETRAKIT_TENANTS dict at the bottom of this file. The shape of the integration
is identical across tenants — only `portal_host` and the per-city watermark
file change.

What this fetches:
  Code cases opened on or after a watermark date. The portal's Search By
  options expose `STARTED` as a date column, paired with the `AT LEAST`
  operator that's equivalent to ">=" — so on incremental runs we just ask
  for everything opened since the last successful run.

How it fetches:
  eTRAKiT is an ASP.NET WebForms application with Telerik UpdatePanels. The
  search form does not have a public JSON endpoint and the postback dance
  with __VIEWSTATE / __EVENTVALIDATION is brittle across pages. So we drive
  the page with Playwright (same pattern as miami_dade_unincorporated.py)
  and click the built-in "Export to Excel" button. The download is a
  RadGridExport.csv containing every result row from the current search.

What this does NOT fetch:
  Owner name + mailing address + folio. The result grid carries only the
  property address. Each row is enriched in a second pass via the
  Miami-Dade Property Appraiser address search (lookup.property_appraiser)
  before mailing.

Run manually:
    python -m connectors.etrakit pinecrest
    python -m connectors.etrakit pinecrest --since 2026-05-01
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from db import init_db, upsert_violations
from parser import build_record

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATERMARK_DIR = Path(os.environ.get("DATA_DIR") or (PROJECT_ROOT / "data"))
AUDIT_ROOT = PROJECT_ROOT / "audit"

DEFAULT_COLD_START_DAYS = 90
PAGE_LOAD_TIMEOUT_MS = 45_000
POST_CLICK_WAIT_MS = 4_000


@dataclass(frozen=True)
class EtrakitTenant:
    """Per-city config. Add an entry to ETRAKIT_TENANTS to onboard a city."""
    source:       str    # canonical DB key, lowercase snake_case
    pretty_name:  str
    portal_host:  str    # e.g. "pine-trk.aspgov.com"


ETRAKIT_TENANTS: dict[str, EtrakitTenant] = {
    "pinecrest": EtrakitTenant(
        source="pinecrest",
        pretty_name="Village of Pinecrest",
        portal_host="pine-trk.aspgov.com",
    ),
    # To onboard another eTRAKiT city: confirm the portal hostname (most are
    # *-trk.aspgov.com) and add a TylerTenant-style block here.
}


# ---------- watermark ----------

def _watermark_path(source: str) -> Path:
    return WATERMARK_DIR / f"{source}_etrakit_watermark.txt"


def _read_watermark(source: str) -> date | None:
    p = _watermark_path(source)
    if not p.exists():
        return None
    try:
        return date.fromisoformat(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def _save_watermark(source: str, d: date) -> None:
    p = _watermark_path(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(d.isoformat(), encoding="ascii")


# ---------- portal driver ----------

def _case_search_url(t: EtrakitTenant) -> str:
    return f"https://{t.portal_host}/eTRAKiT/Search/case.aspx"


def _export_csv(t: EtrakitTenant, since: date, *, log: logging.Logger) -> Path:
    """
    Drive the case-search form, run STARTED >= since, click Export-to-Excel,
    save the download under audit/<source>/<timestamp>-cases.csv, return path.

    Raises RuntimeError on any unrecoverable portal failure.
    """
    audit_dir = AUDIT_ROOT / t.source
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out_path = audit_dir / f"{stamp}-cases.csv"

    url = _case_search_url(t)
    since_str = since.strftime("%m/%d/%Y")
    log.info("loading %s (since=%s)", url, since_str)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)
            page.goto(url, wait_until="networkidle")

            # The Search By dropdown is plain HTML <select>. Selecting it
            # triggers a server postback that swaps the value-input control.
            page.select_option(
                'select[name="ctl00$cplMain$ddSearchBy"]',
                value="Case_Main.STARTED",
            )
            page.wait_for_load_state("networkidle")

            page.select_option(
                'select[name="ctl00$cplMain$ddSearchOper"]',
                value="AT LEAST",
            )
            page.wait_for_load_state("networkidle")

            # txtSearchString is a plain RadTextBox here, no datepicker; it
            # accepts M/D/YYYY or MM/DD/YYYY.
            page.fill("#cplMain_txtSearchString", since_str)
            page.click('input[name="ctl00$cplMain$btnSearch"]')
            page.wait_for_timeout(POST_CLICK_WAIT_MS)
            page.wait_for_load_state("networkidle")

            # Sanity: was the search a no-op? The grid renders `rgRow` /
            # `rgAltRow` class names on result rows; an empty grid has neither.
            row_count = page.locator(
                'table[id*="rgSearchRslts"] tr.rgRow, '
                'table[id*="rgSearchRslts"] tr.rgAltRow'
            ).count()
            if row_count == 0:
                log.info("portal returned no results since %s", since_str)
                # Write an empty CSV so audit + downstream still see "we ran".
                out_path.write_text(
                    "CASE NUMBER,CASE NAME,STARTED,Case Type,Case Sub Type,"
                    "ADDRESS,STATUS,RECORDID\n",
                    encoding="utf-8",
                )
                return out_path

            # Click Export-to-Excel — it actually streams a CSV named
            # RadGridExport.csv. The download contains every row across all
            # pages in the current search (Telerik exports the underlying
            # dataset, not just the visible page). Use the name-based selector
            # because the rendered DOM id varies (cplMain_btnExportToExcel
            # without the ctl00_ prefix vs. ctl00_cplMain_btnSearch with it).
            try:
                with page.expect_download(timeout=30_000) as dl_info:
                    page.click('input[name="ctl00$cplMain$btnExportToExcel"]')
                d = dl_info.value
                d.save_as(str(out_path))
            except PWTimeout as e:
                raise RuntimeError(f"Export-to-Excel timed out: {e}")

            log.info("exported %s (%d bytes)", out_path.name, out_path.stat().st_size)
            return out_path
        finally:
            browser.close()


# ---------- parsing ----------

# Map the eTRAKiT export columns to canonical violation fields. CASE NAME is
# the human-readable description (e.g. "FENCE NO PERMIT - 12345 SW...") and
# is what the keyword regex runs against to gate in-scope vs nuisance rows.
COLUMN_MAP = {
    "CASE NUMBER":   "case_number",
    "CASE NAME":     "alleged_violation",
    "STARTED":       "open_date",
    "Case Type":     "case_type",
    "Case Sub Type": "case_subtype",   # not a canonical field; folded in below
    "ADDRESS":       "property_address",
    "STATUS":        "activity",
}


def _records_from_csv(t: EtrakitTenant, csv_path: Path,
                      *, log: logging.Logger) -> tuple[list[dict], int]:
    """Return (in-scope records, dropped count) parsed from the export CSV."""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    log.info("parsed %d rows from %s", len(df), csv_path.name)

    records: list[dict] = []
    dropped = 0
    for _, row in df.iterrows():
        case_no = (row.get("CASE NUMBER") or "").strip()
        if not case_no:
            continue
        # Combine Case Type + Sub Type into a single readable case_type cell
        # so the operator review screen shows the full chapter+subtype context
        # without us having to add another DB column.
        case_type_combined = " — ".join(
            p for p in (
                (row.get("Case Type") or "").strip(),
                (row.get("Case Sub Type") or "").strip(),
            ) if p
        )
        fields = {
            "case_type":         case_type_combined,
            "open_date":         row.get("STARTED"),
            "property_address":  row.get("ADDRESS"),
            "activity":          row.get("STATUS"),
            "alleged_violation": row.get("CASE NAME"),
            "comments":          "NEEDS_OWNER_LOOKUP",
        }
        rec = build_record(
            source=t.source,
            case_number=case_no,
            fields=fields,
            raw_source_file=f"etrakit:{csv_path.name}",
        )
        if rec is None:
            dropped += 1
        else:
            records.append(rec)
    return records, dropped


# ---------- owner enrichment ----------

def _enrich_owners_for(source: str, *, log: logging.Logger) -> int:
    """
    For each row in this source still flagged NEEDS_OWNER_LOOKUP, look up the
    property address against the Miami-Dade Property Appraiser to fill folio
    + owner_full_name + owner_mailing_address.

    eTRAKiT exports don't carry folio either, so we fall back to address-based
    PA search. Address matches in PA are best-effort — partial hits get
    written, ambiguous hits are skipped (the row stays NEEDS_OWNER_LOOKUP and
    will retry next run).
    """
    import sqlite3
    from db import DB_PATH
    from lookup.property_appraiser import lookup as pa_folio_lookup, address_to_folio

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source, case_number, property_address, folio_number, comments
              FROM violations
             WHERE source = ?
               AND ( owner_full_name      IS NULL OR owner_full_name      = ''
                  OR owner_mailing_address IS NULL OR owner_mailing_address = ''
                  OR comments LIKE '%NEEDS_OWNER_LOOKUP%' )
            """,
            (source,),
        ).fetchall()

    if not rows:
        return 0

    log.info("enriching %d row(s) via ArcGIS + Property Appraiser", len(rows))
    enriched = 0
    for r in rows:
        address = (r["property_address"] or "").strip()
        folio = (r["folio_number"] or "").strip()
        owner_name = ""
        mailing = ""

        # Resolve folio from address via the ArcGIS parcel layer if needed.
        # The PA proxy's address-search operation silently returns a default
        # record on no-match, which is unsafe for automated enrichment — the
        # GIS layer is exact-match and trustworthy.
        if not folio and address:
            try:
                folio = address_to_folio(address) or ""
            except Exception as e:
                log.warning("GIS address lookup error for %s/%s %r: %s",
                            r["source"], r["case_number"], address, e)

        # Hit the folio lookup for owner + mailing address.
        if folio:
            try:
                info = pa_folio_lookup(folio)
            except Exception as e:
                log.warning("PA folio-lookup error for %s/%s folio %s: %s",
                            r["source"], r["case_number"], folio, e)
                info = None
            if info and info.found():
                owner_name = info.owner_full_name
                mailing = info.owner_mailing_address

        if not (folio and owner_name and mailing):
            # Still missing pieces — leave the row flagged for next run.
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
                   SET folio_number          = ?,
                       owner_full_name       = ?,
                       owner_mailing_address = ?,
                       comments              = ?
                 WHERE source = ? AND case_number = ?
                """,
                (folio, owner_name, mailing, new_comments,
                 r["source"], r["case_number"]),
            )
        enriched += 1
        time.sleep(0.25)  # be polite to the PA

    log.info("enriched %d/%d", enriched, len(rows))
    return enriched


# ---------- main ----------

def run(source: str, *, since_date: date | None = None) -> dict:
    """
    Run the eTRAKiT connector for one tenant.

    On first run (no watermark), pulls cases STARTED in the last
    DEFAULT_COLD_START_DAYS days. On subsequent runs, pulls cases STARTED on
    or after the watermark.
    """
    if source not in ETRAKIT_TENANTS:
        raise KeyError(f"Unknown eTRAKiT tenant: {source!r}. "
                       f"Known: {sorted(ETRAKIT_TENANTS)}")
    t = ETRAKIT_TENANTS[source]

    log = logging.getLogger(t.source)
    init_db()

    if since_date is None:
        watermark = _read_watermark(t.source)
        if watermark is None:
            since_date = date.today() - timedelta(days=DEFAULT_COLD_START_DAYS)
            log.info("cold start: pulling last %d days (since %s)",
                     DEFAULT_COLD_START_DAYS, since_date)
        else:
            # Re-pull the watermark day itself in case the previous run
            # caught only a partial day's worth of cases.
            since_date = watermark
            log.info("incremental run: pulling since watermark %s", since_date)

    csv_path = _export_csv(t, since_date, log=log)
    records, dropped = _records_from_csv(t, csv_path, log=log)
    log.info("after keyword filter: %d in scope, %d dropped", len(records), dropped)

    inserted, updated = upsert_violations(records) if records else (0, 0)
    log.info("DB upsert: %d inserted, %d updated", inserted, updated)

    enriched = _enrich_owners_for(t.source, log=log)

    # Advance the watermark to the highest open_date we saw this run, so the
    # next run picks up cleanly from there. If we got zero rows we leave the
    # watermark alone — tomorrow's run will retry the same window.
    new_watermark: date | None = None
    if records:
        try:
            max_open = max(r["open_date"] for r in records if r.get("open_date"))
            new_watermark = date.fromisoformat(max_open)
        except (TypeError, ValueError):
            new_watermark = None
    if new_watermark:
        prior = _read_watermark(t.source)
        if prior is None or new_watermark > prior:
            _save_watermark(t.source, new_watermark)
            log.info("watermark advanced %s -> %s", prior, new_watermark)

    return {
        "source":          t.source,
        "fetched":         len(records) + dropped,
        "in_scope":        len(records),
        "dropped":         dropped,
        "inserted":        inserted,
        "updated":         updated,
        "enriched":        enriched,
        "watermark_after": str(_read_watermark(t.source) or ""),
        "export_path":     str(csv_path),
    }


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("source", choices=sorted(ETRAKIT_TENANTS))
    p.add_argument("--since",
                   help="Override watermark; only collect cases STARTED on or "
                        "after this YYYY-MM-DD date.")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    since = date.fromisoformat(args.since) if args.since else None
    summary = run(args.source, since_date=since)
    print()
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _cli()
