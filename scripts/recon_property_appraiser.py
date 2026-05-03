"""
Recon for the Miami-Dade Property Appraiser folio lookup.

We need owner name + mailing address per folio so the Tyler-sourced rows
(currently NEEDS_OWNER_LOOKUP) can mail.

Strategy:
  1. Drive the public Property Search by folio number for a real Homestead
     case (1078130040420 — 637 SW 7TH ST, owner unknown to us).
  2. Capture every JSON XHR the site fires.
  3. Identify the call(s) that carry owner + mailing address.
  4. Print enough of the response that we can map fields cleanly.

Writes everything to:
    audit/papp_recon/<timestamp>/
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / "papp_recon"

CANARY_FOLIO = "10-7813-004-0420"   # 637 SW 7TH ST, Homestead — visible in test data
SEARCH_URL = "https://www.miamidade.gov/Apps/PA/PropertySearch/"


def main(folio: str = CANARY_FOLIO) -> int:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIT_DIR / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            ct = resp.headers.get("content-type", "")
            try:
                body = resp.json() if "json" in ct.lower() else None
            except Exception:
                body = None
            captured.append({
                "url":      resp.url,
                "method":   req.method,
                "status":   resp.status,
                "ct":       ct,
                "post":     req.post_data,
                "response": body,
            })

        page.on("response", on_response)

        print(f"[1] loading {SEARCH_URL}")
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out / "01_landing.png"), full_page=True)

        # The PA site has tabs for Address / Owner / Folio / Subdivision.
        # Click the Folio tab.
        print("[2] selecting Folio tab")
        for sel in ("a:has-text('Folio')", "button:has-text('Folio')",
                    "[role=tab]:has-text('Folio')", "text=Folio"):
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    loc.click()
                    page.wait_for_timeout(500)
                    print(f"    clicked: {sel}")
                    break
            except Exception:
                continue

        # Find a folio input and submit
        print(f"[3] entering folio {folio}")
        candidates = (
            "input[placeholder*='Folio' i]",
            "input[name*='folio' i]",
            "input[id*='folio' i]",
            "input[aria-label*='Folio' i]",
        )
        filled = False
        for sel in candidates:
            inp = page.locator(sel).first
            try:
                if inp.count() and inp.is_visible():
                    inp.fill(folio)
                    filled = True
                    print(f"    filled via: {sel}")
                    break
            except Exception:
                continue
        if not filled:
            # Last-ditch: any visible text input
            for inp in page.locator("input[type=text]").all():
                try:
                    if inp.is_visible():
                        inp.fill(folio); filled = True
                        print("    filled fallback text input")
                        break
                except Exception:
                    pass

        page.screenshot(path=str(out / "02_folio_entered.png"), full_page=True)

        print("[4] submitting search")
        for sel in ("button:has-text('Search')", "button[type=submit]",
                    "input[type=submit]", "[role=button]:has-text('Search')"):
            btn = page.locator(sel).first
            try:
                if btn.count() and btn.is_visible():
                    btn.click()
                    break
            except Exception:
                continue

        page.wait_for_load_state("networkidle", timeout=45_000)
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / "03_results.png"), full_page=True)
        try:
            (out / "results.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

        browser.close()

    # Save the network log
    (out / "network.json").write_text(
        json.dumps(captured, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n[5] captured {len(captured)} XHRs")
    json_resps = [c for c in captured if c.get("response") is not None]
    print(f"    JSON responses: {len(json_resps)}")
    print()
    for c in json_resps:
        url = c["url"]
        # Heuristic: highlight the response that mentions Owner / Mailing
        body_str = json.dumps(c.get("response") or {}, default=str)[:2000]
        signals = sum(1 for kw in ("Owner", "Mailing", "MailAddr",
                                   "MailingAddress", "TaxAddress")
                      if kw in body_str)
        marker = "  <-- looks like owner data" if signals >= 2 else ""
        print(f"  {c['status']} {c['method']} {url}{marker}")

    print(f"\n[6] artifacts in {out}")
    return 0


if __name__ == "__main__":
    f = sys.argv[1] if len(sys.argv) > 1 else CANARY_FOLIO
    raise SystemExit(main(f))
