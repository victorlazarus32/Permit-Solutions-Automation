"""
Open Homestead's Tyler Advanced search panel and dump every option from
the Code Case Type and Code Case Status dropdowns. These are the canonical
case classifications Homestead uses, with their GUIDs (which we'll need to
filter on later).

Output:
    audit/homestead_recon/<stamp>/taxonomy.json
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = PROJECT_ROOT / "audit" / "homestead_recon"
PORTAL = "https://cityofhomesteadfl-energovweb.tylerhost.net/apps/selfservice"


def main() -> int:
    out_dir = AUDIT_DIR / dt.datetime.now().strftime("taxonomy_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        page.goto(PORTAL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(2500)
        page.get_by_role("link", name="Search").first.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        page.wait_for_timeout(2000)

        # Pick Code Case scope so the type dropdown is populated
        for s in page.locator("select").all():
            try:
                if s.is_visible() and s.is_enabled():
                    s.select_option(label="Code Case")
                    break
            except Exception:
                continue

        # Open Advanced panel
        page.locator("button:has-text('Advanced')").first.click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out_dir / "advanced.png"), full_page=True)

        # Dump every visible <select> on the page after Advanced opens.
        # Tyler renders Code Case Type and Status as standard <select>s.
        dropdowns: dict[str, list[dict]] = {}
        for s in page.locator("select").all():
            try:
                if not (s.is_visible() and s.is_enabled()):
                    continue
                aria = s.get_attribute("aria-label") or ""
                # Resolve the visible label by walking the DOM
                label_text = s.evaluate(
                    "e => { "
                    " const id = e.id; "
                    " if (id) { const l = document.querySelector('label[for=\"'+id+'\"]'); "
                    "           if (l) return l.innerText.trim(); }"
                    " const wrap = e.closest('.form-group, .row, div'); "
                    " if (wrap) { const lbl = wrap.querySelector('label'); "
                    "             if (lbl) return lbl.innerText.trim(); }"
                    " return ''; "
                    "}"
                ) or aria or "(unlabeled)"
                opts = []
                for o in s.locator("option").all():
                    try:
                        opts.append({
                            "value": o.get_attribute("value") or "",
                            "text":  (o.inner_text() or "").strip(),
                        })
                    except Exception:
                        pass
                if not opts:
                    continue
                # If multiple selects share a label, suffix with index
                key = label_text
                i = 1
                while key in dropdowns:
                    i += 1
                    key = f"{label_text} ({i})"
                dropdowns[key] = opts
            except Exception:
                continue

        browser.close()

    (out_dir / "taxonomy.json").write_text(
        json.dumps(dropdowns, indent=2), encoding="utf-8"
    )
    print(f"Dumped {len(dropdowns)} dropdown(s) to {out_dir}")
    print()
    for label, opts in dropdowns.items():
        print(f"=== {label} ({len(opts)} options) ===")
        for o in opts:
            preview = o["text"][:60]
            val = o["value"][:40]
            print(f"  {preview!r:42}  value={val!r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
