"""
Connector: Tyler EnerGov Civic Self Service (CSS) — public code-case API.

Generic, tenant-aware. Each Tyler-hosted city gets a config block in the
TYLER_TENANTS dict at the bottom of this file. The shape of the integration
is identical across tenants — only the URL prefix, headers, and the
CodeCaseTypeId GUID for "Notice of Violation" change.

What this fetches:
  Notices of Violation only (one of seven case types per tenant). This is
  the category that contains permit/zoning issues like "FENCE NO PERMIT",
  "UNPERMITTED SHED", "WINDOWS NO PERMIT". Other types — Abatement,
  Complaints, Animals, Sanitation, Tickets, Unsafe Structure — are
  excluded at the API level so they never reach our DB or letter pipeline.

What this does NOT fetch:
  Owner name and mailing address. The search endpoint doesn't carry those.
  Each row is flagged NEEDS_OWNER_LOOKUP so the Lob mailer skips it until
  enriched. Owner enrichment via the property appraiser is a separate
  problem.

Quirks confirmed live 2026-05-03 against Homestead:
  - Pages are 1-indexed. PageNumber=0 returns 400.
  - PageSize is silently ignored — Tyler always returns 10 rows per page.
  - SortAscending=false is silently ignored — results come back ascending
    by CaseNumber.keyword.
  - OpenedDateFrom/To is silently ignored when paired with CodeCaseTypeId
    (numHits Elasticsearch error). So we can't filter server-side by date —
    instead, we paginate descending case numbers and stop when we hit our
    watermark.
  - Hammering the endpoint trips a rate-limiter around 50 rapid requests.
    Sleep 0.6s between pages and retry once on ConnectionError.

Run manually:
    python -m connectors.tyler_energov homestead
    python -m connectors.tyler_energov homestead --since CC-25-00100-NOV

Run from morning_run / dashboard:
    from connectors.tyler_energov import run
    summary = run("homestead")
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import requests

from db import init_db, upsert_violations
from parser import build_record

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATERMARK_DIR = PROJECT_ROOT / "data"

# Tyler's request body has 8+ criteria sub-blocks plus per-module sort lists.
# Hand-rebuilding it is fragile (Tyler validates fields silently). We ship a
# verbatim capture from a working request and mutate only what we need.
TEMPLATE_PATH = Path(__file__).resolve().parent / "tyler_energov_template.json"
_TEMPLATE_CACHE: dict | None = None


def _load_template() -> dict:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        _TEMPLATE_CACHE = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return _TEMPLATE_CACHE

# Be polite. Tyler rate-limits aggressive callers around 50 reqs/sec.
PAGE_SLEEP_SEC = 0.6
REQUEST_TIMEOUT = 30
MAX_PAGES_PER_RUN = 600  # Tyler's full catalog is ~290 pages today; 600 is headroom
DEFAULT_FIRST_RUN_PAGES = 30  # cold-start: pull the most recent ~300 cases

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")


@dataclass(frozen=True)
class TylerTenant:
    """Per-city config. Add a new entry to TYLER_TENANTS to onboard a city."""
    source:               str           # canonical DB key, lowercase snake_case
    pretty_name:          str
    portal_host:          str           # e.g. "cityofhomesteadfl-energovweb.tylerhost.net"
    tenant_id:            str           # almost always "1"
    tenant_name:          str           # e.g. "homesteadflprod"
    tenant_url:           str           # often equal to tenant_name
    nov_case_type_id:     str           # GUID of the "Notice of Violation" type for this tenant


TYLER_TENANTS: dict[str, TylerTenant] = {
    "homestead": TylerTenant(
        source="homestead",
        pretty_name="City of Homestead",
        portal_host="cityofhomesteadfl-energovweb.tylerhost.net",
        tenant_id="1",
        tenant_name="homesteadflprod",
        tenant_url="homesteadflprod",
        nov_case_type_id="bc5d91b4-9b93-8e36-f93c-02ca1e74101e",
    ),
    # To onboard another Tyler city:
    #   1. Run scripts/dump_homestead_taxonomy.py against that city's portal
    #      to extract its Notice-of-Violation GUID and tenant headers.
    #   2. Add a TylerTenant entry here.
    #   3. (Optional) Reference the new source key from morning_run.py.
}


def _endpoint(t: TylerTenant) -> str:
    return f"https://{t.portal_host}/apps/selfservice/api/energov/search/search"


def _headers(t: TylerTenant) -> dict:
    return {
        "accept":               "application/json, text/plain, */*",
        "content-type":         "application/json;charset=UTF-8",
        "tenantid":             t.tenant_id,
        "tenantname":           t.tenant_name,
        "tyler-tenanturl":      t.tenant_url,
        "tyler-tenant-culture": "en-US",
        "referer":              f"https://{t.portal_host}/apps/selfservice",
        "user-agent":           USER_AGENT,
    }


def _build_body(t: TylerTenant, *, page_number: int) -> dict:
    """
    Build a request body by deep-copying the captured template and overriding
    only the fields we care about. PageNumber is 1-indexed.
    """
    body = deepcopy(_load_template())
    body["Keyword"]    = ""
    body["ExactMatch"] = False
    cc = body["CodeCaseCriteria"]
    cc["CodeCaseTypeId"] = t.nov_case_type_id
    cc["PageNumber"]     = page_number
    return body


def _post_page(session: requests.Session, t: TylerTenant, *,
               page_number: int, log: logging.Logger) -> list[dict]:
    """One request. Retries once on connection reset."""
    body = _build_body(t, page_number=page_number)
    headers = _headers(t)
    url = _endpoint(t)
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            r = session.post(url, json=body, headers=headers,
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            d = r.json()
            if not d.get("Success"):
                # Tyler returned 200 with Success=false. Log and treat as empty.
                log.warning("page %d: API returned Success=false: %s",
                            page_number, (d.get("ErrorMessage") or "")[:200])
                return []
            return d.get("Result", {}).get("EntityResults", []) or []
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            log.warning("page %d attempt %d failed: %s — retrying",
                        page_number, attempt, e)
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"page {page_number} failed twice: {last_err}")


def _watermark_path(source: str) -> Path:
    return WATERMARK_DIR / f"{source}_tyler_watermark.txt"


def _read_watermark(source: str) -> str | None:
    p = _watermark_path(source)
    if not p.exists():
        return None
    txt = p.read_text(encoding="ascii").strip()
    return txt or None


def _save_watermark(source: str, max_case_number: str) -> None:
    p = _watermark_path(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(max_case_number, encoding="ascii")


# Page-number watermark: the page where the case-number watermark sat on the
# previous run. Tyler returns cases ascending and stable, so we can resume near
# this page instead of restarting at page 1. The case-number filter is still
# the source of truth — this is just a starting-point hint.
RESUME_PAGE_BUFFER = 5  # pages back from saved page, to absorb minor catalog shifts


def _watermark_page_path(source: str) -> Path:
    return WATERMARK_DIR / f"{source}_tyler_watermark_page.txt"


def _read_watermark_page(source: str) -> int | None:
    p = _watermark_page_path(source)
    if not p.exists():
        return None
    try:
        n = int(p.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None
    return max(1, n)


def _save_watermark_page(source: str, page_number: int) -> None:
    p = _watermark_page_path(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(max(1, page_number)), encoding="ascii")


def _normalize_address(addr_obj) -> str | None:
    """Tyler's Address is a sub-object; AddressDisplay is the human-readable string."""
    if isinstance(addr_obj, str) and addr_obj.strip():
        return addr_obj.strip()
    return None


def _row_to_record(*, t: TylerTenant, raw: dict) -> dict | None:
    """
    Map one Tyler search-result row into our canonical violation record.

    The keyword regex (config/keywords.py) runs inside build_record() against
    `alleged_violation`. Rows whose description doesn't mention any in-scope
    work (fence, window, garage, etc.) are dropped here, so trash/mattress/
    nuisance NOVs never enter the DB.
    """
    case_number = raw.get("CaseNumber")
    if not case_number:
        return None

    # Apply date is the case-opened equivalent for Tyler NOVs.
    apply_date = raw.get("ApplyDate")
    final_date = raw.get("FinalDate")

    fields = {
        "case_type":         raw.get("CaseType"),         # always "Notice of Violation"
        "activity":          raw.get("CaseStatus"),       # e.g. "Case Created"
        "open_date":         apply_date,
        "close_date":        final_date,
        "property_address":  raw.get("AddressDisplay") or _normalize_address(raw.get("Address")),
        "folio_number":      raw.get("MainParcel"),
        "alleged_violation": raw.get("Description"),
        "comments":          "NEEDS_OWNER_LOOKUP",       # search API doesn't carry owner
    }

    rec = build_record(
        source=t.source,
        case_number=case_number,
        fields=fields,
        raw_source_file=f"tyler:{t.portal_host}",
    )
    return rec  # build_record handles keyword filter (returns None on no-match)


def fetch_pages(t: TylerTenant, *, max_pages: int,
                since_case_number: str | None = None,
                start_page: int = 1,
                log: logging.Logger) -> tuple[list[dict], int]:
    """
    Paginate the search endpoint and return rows whose CaseNumber is strictly
    greater than `since_case_number`, plus the page number where the watermark
    was crossed (0 if it was never crossed).

    Tyler returns cases sorted ASCENDING by CaseNumber regardless of what we
    request, so on incremental runs we skip the early pages (already-ingested
    cases <= watermark) and start collecting once we cross the watermark.

    `start_page` lets the caller resume near the previous run's crossing page,
    avoiding hundreds of redundant skip-pages each run. Safe to over- or
    under-shoot: the case-number filter still gates what gets collected.

    Stops when:
      - empty page returned (catalog exhausted), or
      - max_pages reached (safety cap).
    """
    seen: set[str] = set()
    out: list[dict] = []
    crossed_watermark = since_case_number is None  # cold start collects everything
    crossed_at_page = 0
    pages_above_watermark = 0
    with requests.Session() as session:
        for page in range(start_page, max_pages + 1):
            try:
                rows = _post_page(session, t, page_number=page, log=log)
            except RuntimeError as e:
                log.error("stopping pagination: %s", e)
                break
            if not rows:
                log.info("page %d empty — catalog exhausted at %d collected rows",
                         page, len(out))
                break

            new_in_page = 0
            for row in rows:
                cn = row.get("CaseNumber")
                if not cn or cn in seen:
                    continue
                seen.add(cn)
                # Strict-greater-than so re-running with the same watermark
                # is a true no-op.
                if since_case_number and cn <= since_case_number:
                    continue
                out.append(row)
                new_in_page += 1

            if not crossed_watermark and new_in_page > 0:
                crossed_watermark = True
                crossed_at_page = page
                log.info("page %d: crossed watermark %s; new rows start here",
                         page, since_case_number)

            if crossed_watermark:
                pages_above_watermark += 1
                if page == 1 or pages_above_watermark % 10 == 0:
                    log.info("page %d: %d new rows (running total %d)",
                             page, new_in_page, len(out))
            else:
                # Pre-watermark skipping phase — log every 25 pages so a
                # mis-sized MAX_PAGES_PER_RUN cap doesn't fail silently.
                if page % 25 == 0:
                    last_cn = rows[-1].get("CaseNumber") if rows else "?"
                    log.info("page %d: still skipping (last seen %s, watermark %s)",
                             page, last_cn, since_case_number)

            time.sleep(PAGE_SLEEP_SEC)

    return out, crossed_at_page


def run(source: str, *, max_pages: int | None = None,
        since_case_number: str | None = None) -> dict:
    """
    Run the Tyler EnerGov connector for one tenant.

    On first run (no watermark), pulls DEFAULT_FIRST_RUN_PAGES pages (~300
    rows). On subsequent runs, paginates the full catalog ascending, skipping
    rows at or below the watermark and collecting everything newer.
    """
    if source not in TYLER_TENANTS:
        raise KeyError(f"Unknown Tyler tenant: {source!r}. "
                       f"Known: {sorted(TYLER_TENANTS)}")
    t = TYLER_TENANTS[source]

    log = logging.getLogger(t.source)
    init_db()

    if since_case_number is None:
        since_case_number = _read_watermark(t.source)
    if max_pages is None:
        # Subsequent runs need to walk the whole ascending catalog to find
        # rows past the watermark, so the cap stays at MAX_PAGES_PER_RUN.
        # Cold starts only need recent rows.
        max_pages = (MAX_PAGES_PER_RUN if since_case_number
                     else DEFAULT_FIRST_RUN_PAGES)

    # Resume near the previous run's crossing page to skip the long ascending
    # walk through pre-watermark cases. Falls back to page 1 on cold start or
    # if the saved page hint is missing.
    saved_page = _read_watermark_page(t.source) if since_case_number else None
    start_page = max(1, saved_page - RESUME_PAGE_BUFFER) if saved_page else 1

    log.info("=== %s Tyler run: max_pages=%d since=%s start_page=%d ===",
             t.pretty_name, max_pages, since_case_number or "(cold start)",
             start_page)

    raw_rows, crossed_at_page = fetch_pages(
        t, max_pages=max_pages, since_case_number=since_case_number,
        start_page=start_page, log=log,
    )
    log.info("fetched %d raw NOV rows", len(raw_rows))

    records = []
    dropped_no_match = 0
    for raw in raw_rows:
        rec = _row_to_record(t=t, raw=raw)
        if rec is None:
            dropped_no_match += 1
        else:
            records.append(rec)

    log.info("after keyword filter: %d in scope, %d dropped",
             len(records), dropped_no_match)

    inserted, updated = upsert_violations(records) if records else (0, 0)
    log.info("DB upsert: %d inserted, %d updated", inserted, updated)

    # Owner enrichment via Property Appraiser. Tyler's search endpoint doesn't
    # carry owner data, so every newly-upserted row is flagged
    # NEEDS_OWNER_LOOKUP. Hit the PA in the same run so the morning flow ends
    # with mailable rows, not pending lookups.
    enriched = _enrich_owners_for(t.source, log=log)

    # Update watermark to the highest case number we've seen this run.
    # We use string max because case numbers are like CC-26-00091-NOV — the
    # year segment dominates the lex order, which is what we want.
    if raw_rows:
        new_high = max(r.get("CaseNumber") for r in raw_rows
                       if r.get("CaseNumber"))
        prior = _read_watermark(t.source)
        if not prior or new_high > prior:
            _save_watermark(t.source, new_high)
            log.info("watermark advanced %s -> %s", prior, new_high)

    # Save the page hint so the next run can skip directly to it.
    if crossed_at_page > 0:
        _save_watermark_page(t.source, crossed_at_page)
        log.info("page hint saved: next run starts near page %d", crossed_at_page)

    return {
        "source":          t.source,
        "fetched":         len(raw_rows),
        "in_scope":        len(records),
        "dropped":         dropped_no_match,
        "inserted":        inserted,
        "updated":         updated,
        "enriched":        enriched,
        "watermark_after": _read_watermark(t.source),
    }


def _enrich_owners_for(source: str, *, log: logging.Logger) -> int:
    """
    Look up owner_full_name + owner_mailing_address against the Miami-Dade
    Property Appraiser for any row in this source still flagged
    NEEDS_OWNER_LOOKUP. Returns the count enriched.
    """
    import sqlite3
    from db import DB_PATH
    from lookup.property_appraiser import lookup as pa_lookup

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT source, case_number, folio_number, comments
              FROM violations
             WHERE source = ?
               AND folio_number IS NOT NULL AND folio_number <> ''
               AND ( owner_full_name      IS NULL OR owner_full_name      = ''
                  OR owner_mailing_address IS NULL OR owner_mailing_address = ''
                  OR comments LIKE '%NEEDS_OWNER_LOOKUP%' )
            """,
            (source,),
        ).fetchall()

    if not rows:
        return 0

    log.info("enriching %d row(s) via Property Appraiser", len(rows))
    enriched = 0
    for r in rows:
        try:
            info = pa_lookup(r["folio_number"])
        except Exception as e:
            log.warning("PA lookup error for %s/%s folio %s: %s",
                        r["source"], r["case_number"], r["folio_number"], e)
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
                 new_comments, r["source"], r["case_number"]),
            )
        enriched += 1
        time.sleep(0.25)  # be polite to the PA service
    log.info("enriched %d/%d", enriched, len(rows))
    return enriched


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("source", choices=sorted(TYLER_TENANTS))
    p.add_argument("--max-pages", type=int)
    p.add_argument("--since",
                   help="Override watermark; only collect cases strictly > "
                        "this CaseNumber (e.g. CC-25-00100-NOV)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    summary = run(args.source,
                  max_pages=args.max_pages,
                  since_case_number=args.since)
    print()
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _cli()
