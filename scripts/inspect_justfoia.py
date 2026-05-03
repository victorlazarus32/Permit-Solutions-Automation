"""
Read-only recon of a JustFOIA public portal.

Visits the search page, hunts for the "Submit a Request" / "New Request" link,
follows it (which on most JustFOIA installs redirects to a login or
"continue as guest" page), and dumps the form structure to stdout.

Usage:
    python -m scripts.inspect_justfoia https://homesteadfl.justfoia.com/publicportal/home/search

Does NOT submit anything. Saves a screenshot of the request form to
audit/justfoia_recon/<host>__<timestamp>.png for visual review.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / "justfoia_recon"


def inspect(url: str) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    host = urlparse(url).hostname or "unknown"
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = AUDIT_DIR / f"{host}__{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"[1] navigating {url}")
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.screenshot(path=str(out_dir / "01_search.png"), full_page=True)
        print(f"    title: {page.title()}")
        print(f"    url:   {page.url}")

        # Find any "submit / new request" CTA on the page
        candidates = page.locator(
            "a:has-text('Submit'), a:has-text('New Request'), "
            "a:has-text('Make a Request'), button:has-text('Submit'), "
            "button:has-text('New Request')"
        )
        count = candidates.count()
        print(f"[2] found {count} candidate Submit/New-Request links")
        for i in range(min(count, 10)):
            el = candidates.nth(i)
            try:
                txt = (el.inner_text() or "").strip()
                href = el.get_attribute("href") or ""
                print(f"    - {txt!r} -> {href!r}")
            except Exception as e:
                print(f"    - (could not read element {i}: {e})")

        # Try the canonical JustFOIA new-request URL directly
        new_request_url = url.rstrip("/").replace("/home/search", "/home/newrequest")
        if "newrequest" not in new_request_url:
            new_request_url = url.rstrip("/").rsplit("/", 1)[0] + "/newrequest"
        print(f"[3] navigating to canonical new-request URL: {new_request_url}")
        page.goto(new_request_url, wait_until="networkidle", timeout=30000)
        page.screenshot(path=str(out_dir / "02_newrequest_landing.png"), full_page=True)
        print(f"    title: {page.title()}")
        print(f"    url after nav: {page.url}")

        # Look for login redirect cues
        body_text = (page.locator("body").inner_text() or "").lower()
        gates = []
        for cue in [
            "sign in", "log in", "login", "register", "create account",
            "continue as guest", "continue without an account",
        ]:
            if cue in body_text:
                gates.append(cue)
        print(f"[4] login/account cues on landing: {gates or 'none visible'}")

        # If there is a "continue as guest" style button, click it
        guest_btn = page.locator(
            "a:has-text('Continue as Guest'), a:has-text('Continue without'), "
            "button:has-text('Continue as Guest'), button:has-text('Continue without')"
        ).first
        if guest_btn.count():
            try:
                print("[5] clicking 'continue as guest' style button")
                guest_btn.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                page.screenshot(path=str(out_dir / "03_after_guest.png"), full_page=True)
            except Exception as e:
                print(f"    failed to click guest button: {e}")

        # JustFOIA pattern: chooser page with one tile per request type.
        # Click the "Public Records Request" tile (non-police) to reach the form.
        tile = page.locator("text=/Public Records Request/i").first
        if tile.count():
            print("[5b] clicking 'Public Records Request' tile")
            try:
                tile.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                # Some JustFOIA flows pop a sign-in dialog. Look for and dismiss/note it.
                if page.locator("text=/Sign in/i").count() and page.locator("text=/Submit as Guest/i").count():
                    print("    sign-in dialog visible -- looking for guest button")
                    g = page.locator("button:has-text('Submit as Guest'), a:has-text('Submit as Guest')").first
                    if g.count():
                        g.click()
                        page.wait_for_load_state("networkidle", timeout=20000)
                page.screenshot(path=str(out_dir / "03b_after_tile.png"), full_page=True)
                print(f"    url after tile click: {page.url}")
            except Exception as e:
                print(f"    tile click failed: {e}")

        # Dump every form on the resulting page. JustFOIA renders fields outside
        # of a <form> wrapper, so also dump page-level inputs as a fallback.
        print("\n[6] FORM FIELD DUMP")
        forms = page.locator("form")
        nforms = forms.count()
        print(f"    forms on page: {nforms}")
        if nforms == 0:
            print("    no <form> tags; falling back to page-level inputs")
            forms = page.locator("body")
            nforms = 1
        for i in range(nforms):
            f = forms.nth(i)
            print(f"\n--- form #{i} ---")
            inputs = f.locator("input, select, textarea")
            n = inputs.count()
            for j in range(n):
                el = inputs.nth(j)
                try:
                    tag = el.evaluate("e => e.tagName.toLowerCase()")
                    name = el.get_attribute("name") or ""
                    typ = el.get_attribute("type") or ""
                    placeholder = el.get_attribute("placeholder") or ""
                    required = el.get_attribute("required") is not None
                    aria = el.get_attribute("aria-label") or ""
                    eid = el.get_attribute("id") or ""
                    label_text = ""
                    if eid:
                        lbl = page.locator(f"label[for='{eid}']").first
                        if lbl.count():
                            label_text = (lbl.inner_text() or "").strip()
                    if not label_text and aria:
                        label_text = aria
                    desc = f"{tag}"
                    if typ:
                        desc += f"[{typ}]"
                    desc += f"  name={name!r}"
                    if placeholder:
                        desc += f"  placeholder={placeholder!r}"
                    if label_text:
                        desc += f"  label={label_text!r}"
                    if required:
                        desc += "  REQUIRED"
                    print(f"    {desc}")
                    # Dump select options
                    if tag == "select":
                        opts = el.locator("option")
                        for k in range(opts.count()):
                            opt = opts.nth(k)
                            print(f"        option: {opt.inner_text()!r} (value={opt.get_attribute('value')!r})")
                except Exception as e:
                    print(f"    (field {j} unreadable: {e})")

        # Final full-page screenshot
        page.screenshot(path=str(out_dir / "04_form_full.png"), full_page=True)
        print(f"\n[7] screenshots saved to {out_dir}")

        browser.close()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://homesteadfl.justfoia.com/publicportal/home/search"
    inspect(url)
