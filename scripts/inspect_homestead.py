"""
Recon for the City of Homestead eGovPlus public portal.

Loads the public permit-status page, captures every XHR/fetch the front-end
makes, dumps the form field structure, takes screenshots, and tries one
no-op submission so we can see whether results come back via a JSON
endpoint (= we can call it directly with requests) or only as
server-rendered HTML (= we need Playwright in the connector).

Run:
    python -m scripts.inspect_homestead

Writes everything to:
    audit/homestead_recon/<timestamp>/
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / "homestead_recon"

URL = "https://egov.cityofhomestead.com/eGovPlus91/permit/perm_status.aspx"


def inspect(url: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = AUDIT_DIR / stamp
    out.mkdir(parents=True, exist_ok=True)

    network: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Capture every request for later analysis. We pay attention to XHR/fetch
        # because that's where a JSON backend would show up.
        def _on_req(req):
            network.append({
                "phase":    "request",
                "method":   req.method,
                "url":      req.url,
                "type":     req.resource_type,
                "headers":  dict(req.headers),
                "post":     req.post_data,
            })

        def _on_resp(resp):
            ct = resp.headers.get("content-type", "")
            network.append({
                "phase":  "response",
                "status": resp.status,
                "url":    resp.url,
                "type":   resp.request.resource_type,
                "ct":     ct,
            })

        page.on("request",  _on_req)
        page.on("response", _on_resp)

        print(f"[1] navigating {url}")
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.screenshot(path=str(out / "01_landing.png"), full_page=True)
        print(f"    title: {page.title()}")
        print(f"    url:   {page.url}")

        # Dump the form structure on the landing page
        print("\n[2] FORM FIELD DUMP")
        forms = page.locator("form")
        nforms = forms.count()
        print(f"    forms on page: {nforms}")
        form_dump = []
        for i in range(nforms):
            f = forms.nth(i)
            entry = {"index": i, "action": f.get_attribute("action"),
                     "method": f.get_attribute("method"), "fields": []}
            inputs = f.locator("input, select, textarea")
            n = inputs.count()
            for j in range(n):
                el = inputs.nth(j)
                try:
                    tag = el.evaluate("e => e.tagName.toLowerCase()")
                    name = el.get_attribute("name") or ""
                    typ  = el.get_attribute("type") or ""
                    plc  = el.get_attribute("placeholder") or ""
                    val  = el.get_attribute("value") or ""
                    eid  = el.get_attribute("id") or ""
                    desc = f"{tag}"
                    if typ: desc += f"[{typ}]"
                    desc += f"  id={eid!r}  name={name!r}"
                    if plc: desc += f"  placeholder={plc!r}"
                    if val and typ != "hidden": desc += f"  value={val!r}"
                    if typ == "hidden": desc += "  HIDDEN"
                    print(f"    {desc}")
                    entry["fields"].append({"tag": tag, "type": typ, "name": name,
                                            "id": eid, "placeholder": plc,
                                            "value_preview": val[:60]})
                    if tag == "select":
                        opts = el.locator("option")
                        for k in range(min(opts.count(), 60)):
                            opt = opts.nth(k)
                            print(f"        option: {(opt.inner_text() or '').strip()!r} "
                                  f"(value={opt.get_attribute('value')!r})")
                except Exception as e:
                    print(f"    (field {j} unreadable: {e})")
            form_dump.append(entry)

        (out / "form_dump.json").write_text(
            json.dumps(form_dump, indent=2), encoding="utf-8"
        )

        # Try a no-op probe: submit with no filters, see what the backend returns
        print("\n[3] probing search behavior (empty filters)")
        try:
            search_btns = page.locator(
                "input[type=submit][value*='Search' i], "
                "button:has-text('Search'), "
                "input[type=button][value*='Search' i]"
            )
            if search_btns.count():
                print(f"    found {search_btns.count()} search button(s); clicking first")
                search_btns.first.click()
                page.wait_for_load_state("networkidle", timeout=30000)
                page.screenshot(path=str(out / "02_after_search.png"), full_page=True)
                print(f"    url after search: {page.url}")
            else:
                print("    no Search button visible on landing; skipping probe")
        except Exception as e:
            print(f"    probe failed: {e}")

        # Save the rendered HTML for offline analysis (look for grids, tables, etc.)
        try:
            html = page.content()
            (out / "rendered_after_search.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

        browser.close()

    # Save the network log
    (out / "network.json").write_text(
        json.dumps(network, indent=2, default=str), encoding="utf-8"
    )

    # Summarize what kind of backend we're dealing with
    print("\n[4] NETWORK SUMMARY")
    xhrs = [n for n in network if n["phase"] == "request" and n.get("type") in ("xhr", "fetch")]
    json_resps = [n for n in network if n["phase"] == "response"
                  and "application/json" in (n.get("ct") or "").lower()]
    aspx_posts = [n for n in network if n["phase"] == "request"
                  and n["method"] == "POST" and ".aspx" in n["url"]]
    print(f"    total requests       : {sum(1 for n in network if n['phase']=='request')}")
    print(f"    xhr/fetch requests   : {len(xhrs)}")
    print(f"    application/json resp: {len(json_resps)}")
    print(f"    .aspx POSTs (postback): {len(aspx_posts)}")
    if xhrs:
        print("    XHR endpoints:")
        seen = set()
        for n in xhrs:
            key = (n["method"], n["url"])
            if key in seen: continue
            seen.add(key)
            print(f"      {n['method']:5s} {n['url']}")
    if json_resps:
        print("    JSON responses:")
        seen = set()
        for n in json_resps:
            if n["url"] in seen: continue
            seen.add(n["url"])
            print(f"      {n['status']} {n['url']}")

    print(f"\n[5] artifacts saved to {out}")
    return out


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else URL
    inspect(url)
