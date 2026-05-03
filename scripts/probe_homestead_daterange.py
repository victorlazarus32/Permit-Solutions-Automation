"""
Replay the Tyler EnerGov search endpoint directly via `requests` using a
date-range filter on CodeCaseCriteria. No browser, no Playwright.

Strategy: load the most recent captured XHR body from
`audit/homestead_recon/<stamp>/captured_xhrs.json`, find the search/search
POST, then mutate only the fields we care about (Keyword, OpenedDateFrom/To,
PageNumber, PageSize) and re-post. Avoids hand-rebuilding Tyler's 400-line
request schema.

Run:
    python -m scripts.probe_homestead_daterange [--days 30] [--page-size 50]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / "homestead_recon"

ENDPOINT = ("https://cityofhomesteadfl-energovweb.tylerhost.net"
            "/apps/selfservice/api/energov/search/search")

HEADERS = {
    "accept":               "application/json, text/plain, */*",
    "content-type":         "application/json;charset=UTF-8",
    "tenantid":             "1",
    "tenantname":           "homesteadflprod",
    "tyler-tenanturl":      "homesteadflprod",
    "tyler-tenant-culture": "en-US",
    "referer":              "https://cityofhomesteadfl-energovweb.tylerhost.net/apps/selfservice",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
}


def load_template_body() -> dict:
    """
    Find the most recent captured search/search request body and return it
    as a deep-copyable template.
    """
    if not AUDIT_DIR.exists():
        raise SystemExit(f"No recon dir at {AUDIT_DIR}. Run scripts/recon_homestead_tyler.py first.")
    candidates = sorted(p for p in AUDIT_DIR.iterdir()
                        if p.is_dir() and (p / "captured_xhrs.json").exists())
    if not candidates:
        raise SystemExit(
            "No captured_xhrs.json under audit/homestead_recon/. "
            "Run scripts/recon_homestead_tyler.py first."
        )
    latest = candidates[-1]
    data = json.loads((latest / "captured_xhrs.json").read_text(encoding="utf-8"))
    for call in data.get("calls", []):
        if (call.get("method") == "POST"
                and "search/search" in call.get("url", "")
                and isinstance(call.get("body"), dict)):
            print(f"Template body loaded from {latest.name}")
            return deepcopy(call["body"])
    raise SystemExit(
        f"No search/search POST body found inside {latest}. "
        "Re-run the recon script."
    )


def build_body(template: dict, *, opened_from: dt.date, opened_to: dt.date,
               page: int, page_size: int,
               case_type_id: str | None = None) -> dict:
    body = deepcopy(template)
    body["Keyword"]    = ""
    body["ExactMatch"] = False
    cc = body["CodeCaseCriteria"]
    cc["OpenedDateFrom"] = opened_from.isoformat()
    cc["OpenedDateTo"]   = opened_to.isoformat()
    if case_type_id:
        cc["CodeCaseTypeId"] = case_type_id
    cc["PageNumber"]     = page
    cc["PageSize"]       = page_size
    cc["SortBy"]         = "CaseNumber.keyword"
    cc["SortAscending"]  = False
    return body


def fetch_page(template: dict, **kw) -> dict:
    body = build_body(template, **kw)
    r = requests.post(ENDPOINT, json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


# Code Case Type GUIDs for Homestead (from dump_homestead_taxonomy.py).
HOMESTEAD_CODE_CASE_TYPES = {
    "abatement":               "555e6954-abf4-05b6-42c4-25d6361eee55",
    "complaints":              "13d5753f-fcdd-cba3-2529-af8ce46d7c58",
    "complaints_animals":      "2237960d-bd45-dab4-9814-9957e4ecabb3",
    "complaints_sanitation":   "0e5510b3-aa08-8f49-6d6d-938f74b65eef",
    "notice_of_violation":     "bc5d91b4-9b93-8e36-f93c-02ca1e74101e",
    "ticket":                  "eb1153e9-8f01-5917-0e56-27babe1b42f9",
    "unsafe_structure":        "c1fcf619-a508-9e41-3571-368d01c967ec",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--type",
                   choices=sorted(HOMESTEAD_CODE_CASE_TYPES.keys()) + ["all"],
                   default="notice_of_violation",
                   help="Filter by Code Case Type (default: notice_of_violation)")
    args = p.parse_args()
    type_id = (None if args.type == "all"
               else HOMESTEAD_CODE_CASE_TYPES[args.type])

    template = load_template_body()

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    print(f"Pulling Homestead code cases opened {start} -> {end}\n")

    all_rows: list[dict] = []
    total = None
    for page in range(args.max_pages):
        try:
            data = fetch_page(template,
                              opened_from=start, opened_to=end,
                              page=page, page_size=args.page_size,
                              case_type_id=type_id)
        except requests.HTTPError as e:
            print(f"page {page} HTTP {e.response.status_code}: "
                  f"{e.response.text[:300]}")
            return 1

        if not data.get("Success"):
            print("Success=false:")
            print(json.dumps(data, indent=2)[:800])
            return 1

        result = data.get("Result") or {}
        rows = result.get("EntityResults") or []
        total = result.get("TotalRecords", total)
        print(f"page {page}: {len(rows):3d} rows  "
              f"(running total: {len(all_rows) + len(rows)} of {total})")
        all_rows.extend(rows)
        if not rows or (total is not None and len(all_rows) >= total):
            break

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = AUDIT_DIR / f"daterange_{args.days}d_{stamp}.json"
    out.write_text(json.dumps({
        "endpoint":      ENDPOINT,
        "opened_from":   start.isoformat(),
        "opened_to":     end.isoformat(),
        "total_records": total,
        "rows_pulled":   len(all_rows),
        "rows":          all_rows,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved {len(all_rows)} rows -> {out.name}")

    print("\n=== CaseType breakdown ===")
    for v, n in Counter(r.get("CaseType") for r in all_rows
                        if r.get("CaseType")).most_common():
        print(f"  {n:4d}  {v}")

    print("\n=== CaseStatus breakdown ===")
    for v, n in Counter(r.get("CaseStatus") for r in all_rows
                        if r.get("CaseStatus")).most_common():
        print(f"  {n:4d}  {v}")

    print("\n=== CaseNumber prefix breakdown ===")
    prefixes = Counter()
    for r in all_rows:
        cn = r.get("CaseNumber") or ""
        prefix = cn.split("-", 1)[0] if "-" in cn else cn[:3]
        prefixes[prefix] += 1
    for v, n in prefixes.most_common():
        print(f"  {n:4d}  {v}")

    print("\n=== Sample descriptions per CaseType ===")
    seen: dict[str, list[str]] = {}
    for r in all_rows:
        ct = r.get("CaseType") or "(none)"
        desc = (r.get("Description") or "").replace("\n", " ").strip()
        if not desc:
            continue
        seen.setdefault(ct, [])
        if len(seen[ct]) < 3:
            seen[ct].append(desc[:140])
    for ct in sorted(seen):
        print(f"\n  {ct}:")
        for d in seen[ct]:
            print(f"    - {d}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
