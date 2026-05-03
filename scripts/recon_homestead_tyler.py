"""
Recon for Homestead's Tyler EnerGov Civic Self Service code-case search.

Goal: confirm we can fetch building/zoning violations for Homestead the same
way we do today for Miami-Dade Unincorporated. Specifically:
  1. Drive the public search UI with a 30-day date range.
  2. Capture the exact /SearchApi XHR the front-end sends (URL, headers, body).
  3. Replay it with `requests` to confirm we don't need a browser at runtime.
  4. Dump every distinct CaseType / SubType / ViolationType the response carries
     so Victor can pick the building/zoning allowlist before any letter goes out.

Writes everything to:
    audit/homestead_recon/<timestamp>/
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / "homestead_recon"

PORTAL = "https://cityofhomesteadfl-energovweb.tylerhost.net/apps/selfservice"
SEARCH_PAGE = f"{PORTAL}#/search"

LOOKBACK_DAYS = 30


def _fmt(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def capture_search_xhr() -> dict:
    """
    Drive the UI through one Code-Cases search and grab the XHR body the
    front-end actually sends.

    Returns a dict with: url, method, headers, body (parsed JSON), response
    (parsed JSON).
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS)

    captured: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Capture every XHR/fetch POST. Tyler search endpoints vary across
        # tenants ("/SearchApi", "/api/...search", "/Search/...") so we cast
        # a wide net and filter afterwards.
        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            if req.method.upper() not in ("POST", "PUT"):
                return
            try:
                body_text = req.post_data or ""
                body = json.loads(body_text) if body_text else None
            except Exception:
                body = body_text
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = None
            captured.setdefault("calls", []).append({
                "url":      resp.url,
                "method":   req.method,
                "headers":  dict(req.headers),
                "body":     body,
                "status":   resp.status,
                "response": resp_json,
            })

        page.on("response", on_response)

        print(f"[1] loading {PORTAL}")
        page.goto(PORTAL, wait_until="networkidle", timeout=60_000)
        # SPA needs a beat after networkidle to actually render route content.
        page.wait_for_timeout(2500)

        # Click the topbar Search link to enter the search hub through the
        # SPA's own routing (more reliable than the hash URL).
        print("[2] clicking topbar Search")
        try:
            page.get_by_role("link", name="Search").first.click(timeout=10_000)
        except Exception:
            page.locator("a:has-text('Search')").first.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        page.wait_for_timeout(2500)
        page.screenshot(path=str(_out_dir() / "01_search_landing.png"), full_page=True)

        # Find the search box by its placeholder (most stable anchor),
        # then walk siblings to find the scope <select> next to it.
        print("[3] locating the public search form")
        search_box = page.get_by_placeholder("Search public records",
                                             exact=False).first
        try:
            search_box.wait_for(state="visible", timeout=15_000)
        except Exception:
            page.screenshot(path=str(_out_dir() / "01b_no_search_box.png"),
                            full_page=True)
            raise RuntimeError("Search box never appeared on the page.")

        # Pick the first <select> on the form (visible + enabled). On Tyler this
        # is the scope picker (All / Permit / Plan / Code Case / ...).
        print("[4] scoping search to Code Case via dropdown")
        scope = None
        for s in page.locator("select").all():
            try:
                if s.is_visible() and s.is_enabled():
                    scope = s
                    break
            except Exception:
                continue
        if scope is None:
            print("    no visible <select> on page; proceeding with default scope")
        else:
            picked = False
            for label in ("Code Case", "Code Cases", "CodeCase"):
                try:
                    scope.select_option(label=label)
                    print(f"    selected scope: {label!r}")
                    picked = True
                    break
                except Exception:
                    continue
            if not picked:
                opts = scope.locator("option").all()
                print("    couldn't pick by label. options on the dropdown:")
                for o in opts:
                    try:
                        print(f"      {(o.inner_text() or '').strip()!r}  "
                              f"value={o.get_attribute('value')!r}")
                    except Exception:
                        pass

        # Tyler's "Exact Phrase" defaults to ON, which makes blank/loose
        # queries return zero rows. Untick it.
        try:
            for cb in page.locator("input[type=checkbox]").all():
                if cb.is_visible() and cb.is_checked():
                    # Only untick the one whose label is "Exact Phrase"
                    handle = cb.evaluate("e => (e.closest('label')?.innerText || "
                                         "document.querySelector(`label[for='${e.id}']`)?.innerText || '')")
                    if "exact" in str(handle).lower():
                        cb.uncheck()
                        print("    unticked Exact Phrase")
                        break
        except Exception:
            pass

        # Open the Advanced panel so date-range fields are exposed.
        print("[5] opening Advanced panel")
        try:
            adv = page.locator("button:has-text('Advanced'), "
                               "[role=button]:has-text('Advanced')").first
            if adv.count():
                adv.click()
                page.wait_for_timeout(800)
                print("    Advanced opened")
        except Exception as e:
            print(f"    couldn't open Advanced: {e}")
        page.screenshot(path=str(_out_dir() / "02b_advanced_open.png"),
                        full_page=True)

        # Fill the Opened-date range. Tyler labels these "Opened From" / "Opened To".
        end_d   = dt.date.today()
        start_d = end_d - dt.timedelta(days=LOOKBACK_DAYS)
        print(f"[6] entering date range {start_d} -> {end_d}")
        for label, val in (("Opened From", _fmt(start_d)),
                           ("Opened To",   _fmt(end_d))):
            try:
                box = page.get_by_label(label, exact=False).first
                box.fill(val)
                box.press("Tab")
            except Exception as e:
                print(f"    couldn't fill {label!r}: {e}")

        print("[7] submitting search")
        clicked = False
        for sel in ("button:has-text('Search')",
                    "input[type=submit][value*='Search' i]",
                    "[role=button]:has-text('Search')"):
            for btn in page.locator(sel).all():
                try:
                    if btn.is_visible() and btn.is_enabled():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            # Fall back to pressing Enter inside the search box
            try:
                search_box.press("Enter")
                print("    pressed Enter as fallback")
            except Exception as e:
                print(f"    no Search button + Enter fallback failed: {e}")

        page.wait_for_load_state("networkidle", timeout=60_000)
        # Give the SPA time to fire the XHR and render results
        page.wait_for_timeout(6000)
        page.screenshot(path=str(_out_dir() / "03_results.png"), full_page=True)

        # Save rendered HTML for offline inspection
        try:
            (_out_dir() / "results.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

        browser.close()

    if not captured.get("calls"):
        print("[!] no /SearchApi calls captured")
        return {}

    # The interesting one is the POST that returned a list of cases.
    posts = [c for c in captured["calls"] if c["method"].upper() == "POST"]
    print(f"[5] captured {len(captured['calls'])} SearchApi response(s); "
          f"{len(posts)} POST(s)")
    return captured


def replay_with_requests(call: dict) -> dict | None:
    """Replay the captured POST with `requests` to confirm we don't need Playwright."""
    print(f"\n[6] replaying {call['method']} {call['url']} via requests")
    headers = dict(call.get("headers") or {})
    # Strip browser-only headers that requests will set itself
    for h in ("host", "content-length", "connection", "accept-encoding"):
        headers.pop(h, None)
    body = call.get("body")
    try:
        if isinstance(body, dict):
            r = requests.post(call["url"], json=body, headers=headers, timeout=30)
        else:
            r = requests.post(call["url"], data=body, headers=headers, timeout=30)
    except Exception as e:
        print(f"    replay error: {e}")
        return None
    print(f"    status: {r.status_code}")
    try:
        return r.json()
    except Exception:
        print("    response was not JSON; first 500 chars:")
        print(r.text[:500])
        return None


def summarize_response(resp_json: dict | list) -> None:
    """Print top-level shape, total count, distinct CaseType/SubType/etc."""
    print("\n[7] RESPONSE SUMMARY")
    if isinstance(resp_json, dict):
        keys = list(resp_json.keys())
        print(f"    top-level keys: {keys}")
        # Tyler responses typically come back like
        # { "Result": { "EntityResults": [...] }, "TotalRecords": N }
        # but the shape varies. Try a few common locations.
        rows = None
        for path in (("Result", "EntityResults"),
                     ("EntityResults",),
                     ("Items",), ("data",), ("Data",), ("Cases",)):
            cur = resp_json
            for k in path:
                cur = cur.get(k) if isinstance(cur, dict) else None
                if cur is None: break
            if isinstance(cur, list):
                rows = cur
                print(f"    rows found at: {' -> '.join(path)} (n={len(rows)})")
                break
        if rows is None:
            print("    no row list found; raw response saved to disk")
            return
    elif isinstance(resp_json, list):
        rows = resp_json
        print(f"    response is a bare list (n={len(rows)})")
    else:
        print(f"    unexpected response type: {type(resp_json)}")
        return

    if not rows:
        print("    zero rows in window. Either nothing opened in 30 days, or "
              "the search criteria didn't carry through. Look at the captured "
              "request body in audit/.")
        return

    # Show field names on the first row
    print(f"\n    FIRST-ROW FIELDS:")
    for k, v in (rows[0].items() if isinstance(rows[0], dict) else []):
        prev = str(v)[:60].replace("\n", " ")
        print(f"      {k}: {prev}")

    # Distinct values for the classification fields we care about
    for field in ("CaseType", "CaseSubType", "ViolationType", "Status",
                  "CaseStatus", "Description"):
        values = [r.get(field) for r in rows if isinstance(r, dict) and r.get(field)]
        if not values:
            continue
        c = Counter(values)
        print(f"\n    {field}  ({len(c)} distinct)")
        for v, n in c.most_common(40):
            preview = str(v)[:80]
            print(f"      {n:4d}  {preview}")


_OUT_DIR_CACHE = {}
def _out_dir() -> Path:
    if "p" not in _OUT_DIR_CACHE:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        d = AUDIT_DIR / stamp
        d.mkdir(parents=True, exist_ok=True)
        _OUT_DIR_CACHE["p"] = d
    return _OUT_DIR_CACHE["p"]


def main() -> int:
    out = _out_dir()
    print(f"writing artifacts to {out}\n")

    captured = capture_search_xhr()
    if not captured:
        print("FAIL: no SearchApi calls captured. See screenshots in audit/.")
        return 1

    # Save full capture
    (out / "captured_xhrs.json").write_text(
        json.dumps(captured, indent=2, default=str), encoding="utf-8"
    )

    posts = [c for c in captured["calls"] if c["method"].upper() == "POST"]
    if not posts:
        # Some Tyler search APIs are GET. Just take the last call.
        last = captured["calls"][-1]
        resp = last.get("response")
    else:
        last = posts[-1]
        resp = replay_with_requests(last)
        if resp is None:
            # Fall back to the response Playwright already captured
            resp = last.get("response")

    if resp is None:
        print("FAIL: could not get a response body to summarize.")
        return 1

    (out / "response_sample.json").write_text(
        json.dumps(resp, indent=2, default=str), encoding="utf-8"
    )

    summarize_response(resp)
    print(f"\nartifacts: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
