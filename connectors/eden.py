"""
Connector: Tyler Eden (EdenWebNet) — public permit search.

This is the LEGACY Tyler product (not Tyler EnerGov / CSS). Older Florida
cities — Palmetto Bay, plus a handful of others — still run Eden as their
public permit-and-violations search.

Tenant-aware: ETRAKIT_TENANTS-style dict at the bottom. Each city has its
own portal_host; the search-form field names are stable across deployments.

What this fetches:
  Pending permits in PSS-trade categories (fence, window, door, garage,
  roof, shed, pergola). "Pending" in Eden means the owner pulled a permit
  but never finaled it — every row is a stalled permit, which is exactly
  the lead profile PSS markets to (helping owners close out expired
  permits or restart abandoned work).

  Why not violations? Palmetto Bay's Eden carries only 2 stale "Pending"
  code-violation rows from 2015 — the city does not use Eden for active
  code enforcement. The real PSS signal lives on the permits side.

Search shape (discovered live 2026-06-03 against Palmetto Bay):
  - One <select> field `pmPermit..PERMIT_TYPE_CODE` with ~136 type codes.
  - One radio group `pmPermit..APPROVAL_STATE` with values pending /
    issued / approved / final / * (= All).
  - Date inputs `APPLICATION_DATE` and `ISSUE_DATE` accept only single
    MM/DD/YYYY values — no range operators, no comparison prefixes.
  - When the result set exceeds the display threshold (~200 rows), Eden
    silently bounces back to the form with a "would return too many"
    yellow banner. The TypeCode + Status=Pending combo lands cleanly
    under that ceiling for PSS-trade types.

What this does NOT fetch:
  Owner name + mailing address + folio. The result grid carries only the
  property address. Each row is enriched in a second pass via the ArcGIS
  parcel layer + Property Appraiser folio lookup (same path Pinecrest
  uses).

Run manually:
    python -m connectors.eden palmetto_bay
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from db import init_db, upsert_violations
from parser import build_record

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATERMARK_DIR = Path(os.environ.get("DATA_DIR") or (PROJECT_ROOT / "data"))
AUDIT_ROOT = PROJECT_ROOT / "audit"

PAGE_LOAD_TIMEOUT_MS = 45_000
POST_SUBMIT_WAIT_MS = 2_500


@dataclass(frozen=True)
class EdenTenant:
    """Per-city config. Add an entry to EDEN_TENANTS to onboard a city."""
    source:      str
    pretty_name: str
    portal_host: str          # e.g. "eden.palmettobay-fl.gov"


# PSS trade-scope permit type codes (the city-side names happen to be uniform
# across Tyler Eden deployments — these codes are reused tenant-to-tenant).
# Each entry: (permit_type_code, human_label). Adding/removing a code here
# changes what every Eden tenant pulls — this is intentional because PSS
# scope is the same regardless of city.
SCOPE_PERMIT_TYPES: list[tuple[str, str]] = [
    ("balum",  "ALUMINUM/PICKET/PVC FENCE"),
    ("biron",  "CBS/IRON FENCE/PRE-CAST"),
    ("bchlk",  "CHAINLINK FENCE"),
    ("bwoodf", "WOOD FENCE"),
    ("bwnd",   "WINDOWS/DOORS"),
    ("bfdoor", "FRONT/SIDE DOORS"),
    ("bgar",   "GARAGE DOOR"),
    ("bshrf",  "SHINGLE ROOF"),
    ("bmetrf", "METAL ROOF"),
    ("btile",  "TILE ROOF"),
    ("brfrep", "ROOF REPAIR"),
    ("bfroof", "FLAT ROOF / LOW SLOPE"),
    ("bshed",  "SHED"),
    ("bnpav",  "GAZEBO / PAVILLION / PERGOLA"),
]


EDEN_TENANTS: dict[str, EdenTenant] = {
    "palmetto_bay": EdenTenant(
        source="palmetto_bay",
        pretty_name="Village of Palmetto Bay",
        portal_host="eden.palmettobay-fl.gov",
    ),
}


# ---------- portal driver ----------

def _search_url(t: EdenTenant) -> str:
    return (f"https://{t.portal_host}/EdenWebNet/Default.aspx"
            f"?Build=PM.pmPermit.SearchForm&utask=normalview")


_PERMIT_NO_RE = re.compile(r"^[A-Z]+-\d{4}-\d+$")


def _classify_response(body: str) -> str:
    """Map Eden's possible response states to a label."""
    if "Invalid Search criteria" in body:           return "INVALID"
    if "would return too many records" in body:     return "TOO_MANY"
    if "Your search returned no records" in body:   return "NO_RECORDS"
    return "RESULTS"


def _scrape_pending(page, t: EdenTenant, permit_code: str,
                    *, log: logging.Logger) -> list[dict]:
    """
    Run a single search (TypeCode=permit_code, Status=Pending) and return
    parsed result rows as dicts. Returns [] for any non-RESULTS verdict.
    """
    page.goto(_search_url(t), wait_until="networkidle")
    page.select_option('select[name="pmPermit..PERMIT_TYPE_CODE"]', value=permit_code)
    page.locator('input[type="radio"][value="pending"]').first.check(force=True)
    page.click('input[name="Button"][value="Search for Permits"]')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(POST_SUBMIT_WAIT_MS)

    verdict = _classify_response(page.locator("body").inner_text())
    if verdict != "RESULTS":
        log.info("%s: %s", permit_code, verdict.lower())
        return []

    # Each permit-number link in the result grid points at a detail page
    # whose URL embeds the row's internal PERMIT_ID. Anchors whose visible
    # text matches the permit-number pattern are exactly the result rows.
    rows: list[dict] = []
    anchors = page.locator('a[href*="PERMIT_ID"]').all()
    for a in anchors:
        permit_no = (a.inner_text() or "").strip()
        if not _PERMIT_NO_RE.match(permit_no):
            continue
        # Walk up to the <tr>, then read each <td> in order.
        tr = a.locator("xpath=ancestor::tr[1]")
        cells = [c.inner_text().strip() for c in tr.locator("td").all()]
        # Expected: [permit#, app date, address, type, description, fees]
        if len(cells) < 6:
            continue
        rows.append({
            "permit_no":  cells[0],
            "app_date":   cells[1],
            "address":    cells[2],
            "type_label": cells[3],
            "description": cells[4],
            "fees_due":   cells[5],
        })
    log.info("%s: %d pending rows", permit_code, len(rows))
    return rows


def _scrape_all_scope(t: EdenTenant, *, log: logging.Logger) -> tuple[list[dict], Path]:
    """
    Iterate every PSS-trade permit type, scrape its pending list, return the
    merged row list plus the audit file path where the raw CSV was written.
    """
    audit_dir = AUDIT_ROOT / t.source
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    audit_path = audit_dir / f"{stamp}-pending.csv"

    all_rows: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)
            for code, label in SCOPE_PERMIT_TYPES:
                try:
                    rows = _scrape_pending(page, t, code, log=log)
                except PWTimeout as e:
                    log.warning("%s: timed out (%s) — skipping", code, e)
                    continue
                except Exception as e:
                    log.warning("%s: scrape error (%s) — skipping", code, e)
                    continue
                # Tag each row with the human label so the audit file shows
                # which search produced it (different code => different label).
                for r in rows:
                    r["scope_label"] = label
                all_rows.extend(rows)
        finally:
            browser.close()

    # Write the audit CSV. Keep it simple — one row per result, columns in
    # display order. This file is the human-debuggable source-of-truth for
    # what the connector actually pulled.
    if all_rows:
        import csv
        with audit_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(
                fh,
                fieldnames=["permit_no", "app_date", "address", "type_label",
                            "description", "fees_due", "scope_label"],
            )
            w.writeheader()
            w.writerows(all_rows)
    else:
        audit_path.write_text(
            "permit_no,app_date,address,type_label,description,fees_due,scope_label\n",
            encoding="utf-8",
        )

    return all_rows, audit_path


# ---------- parsing ----------

def _records_from_rows(t: EdenTenant, rows: list[dict],
                       *, log: logging.Logger) -> list[dict]:
    """Build canonical violation records from scraped Eden rows."""
    records: list[dict] = []
    for r in rows:
        case_no = r["permit_no"]
        if not case_no:
            continue
        # Permit Type is already PSS-scope (we only searched scope codes), so
        # bypass the keyword regex — it would miss compound descriptions like
        # "GAZEBO 12X10 SCREEN ROOF" or "FBC 2023 8TH EDITION".
        fields = {
            "case_type":         r.get("type_label"),
            "open_date":         r.get("app_date"),
            "property_address":  r.get("address"),
            "activity":          "Pending",
            "alleged_violation": r.get("description"),
            "comments":          "NEEDS_OWNER_LOOKUP",
        }
        rec = build_record(
            source=t.source,
            case_number=case_no,
            fields=fields,
            raw_source_file=f"eden:{t.portal_host}",
            skip_filter=True,
        )
        if rec is not None:
            # Stamp the trade scope (the search-input label) into
            # matched_keywords so the report cross-tab continues to work.
            rec["matched_keywords"] = (r.get("scope_label") or "").lower()
            records.append(rec)
    return records


# ---------- owner enrichment (shared with eTRAKiT pattern) ----------

def _enrich_owners_for(source: str, *, log: logging.Logger) -> int:
    """Address -> folio via ArcGIS; folio -> owner+mailing via Property Appraiser."""
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

        if not folio and address:
            try:
                folio = address_to_folio(address) or ""
            except Exception as e:
                log.warning("GIS address lookup error for %s/%s %r: %s",
                            r["source"], r["case_number"], address, e)

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
        time.sleep(0.25)

    log.info("enriched %d/%d", enriched, len(rows))
    return enriched


# ---------- main ----------

def run(source: str) -> dict:
    """Run the Eden connector for one tenant."""
    if source not in EDEN_TENANTS:
        raise KeyError(f"Unknown Eden tenant: {source!r}. "
                       f"Known: {sorted(EDEN_TENANTS)}")
    t = EDEN_TENANTS[source]

    log = logging.getLogger(t.source)
    init_db()

    log.info("=== %s Eden run: %d scope-type searches ===",
             t.pretty_name, len(SCOPE_PERMIT_TYPES))

    rows, audit_path = _scrape_all_scope(t, log=log)
    log.info("fetched %d total pending rows across all PSS-trade types",
             len(rows))

    records = _records_from_rows(t, rows, log=log)
    log.info("upserting %d records", len(records))

    inserted, updated = upsert_violations(records) if records else (0, 0)
    log.info("DB upsert: %d inserted, %d updated", inserted, updated)

    enriched = _enrich_owners_for(t.source, log=log)

    return {
        "source":     t.source,
        "fetched":    len(rows),
        "in_scope":   len(records),  # in this connector, fetched == in_scope
        "dropped":    len(rows) - len(records),
        "inserted":   inserted,
        "updated":    updated,
        "enriched":   enriched,
        "audit_path": str(audit_path),
    }


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("source", choices=sorted(EDEN_TENANTS))
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    summary = run(args.source)
    print()
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _cli()
