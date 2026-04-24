"""
Render every mail-ready letter into ONE PDF for review.

Each letter is the full EN + ES bilingual spread (2 pages). Letters are
separated by a page break so printing is clean.

Output: audit/letter_previews/all_letters_<timestamp>.pdf
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db import connect
from lob_sender.derive import derive_for_row

from playwright.sync_api import sync_playwright


TEMPLATE_PATH = PROJECT_ROOT / "templates" / "violation_letter_en.html"
OUT_DIR = PROJECT_ROOT / "audit" / "letter_previews"


READY_SQL = """
    SELECT *
    FROM violations
    WHERE owner_mailing_address IS NOT NULL
      AND owner_full_name      IS NOT NULL
      AND lob_letter_id        IS NULL
      AND (comments NOT LIKE '%NEEDS_OWNER_LOOKUP%' OR comments IS NULL)
    ORDER BY source, first_seen_at ASC
"""


def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_one(template: str, row: dict) -> str | None:
    """Return the rendered HTML for one letter, or None if it couldn't be built."""
    derived = derive_for_row(row)
    if derived["errors"]:
        return None
    html = template
    for key, value in derived["merge_variables"].items():
        html = html.replace("{{" + key + "}}", str(value))
    return html


def extract_body(full_html: str) -> str:
    """Pull just the <body> inner content so we can stack many letters in one doc."""
    start = full_html.lower().find("<body")
    end = full_html.lower().rfind("</body>")
    if start == -1 or end == -1:
        return full_html
    # Skip past the opening <body ...>
    gt = full_html.find(">", start)
    return full_html[gt + 1 : end]


def extract_head(full_html: str) -> str:
    """Grab the <style> block from the first letter — the CSS is the same for all."""
    start = full_html.lower().find("<style")
    end = full_html.lower().find("</style>")
    if start == -1 or end == -1:
        return ""
    return full_html[start : end + len("</style>")]


def build_combined(letters_bodies: list[str], css_block: str) -> str:
    """Stack every letter into one HTML document with a page break between each."""
    head = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Permit Solutions — All Ready-to-Mail Letters</title>
{css_block}
<style>
  .letter-divider {{
    page-break-after: always;
    break-after: page;
  }}
</style>
</head>
<body>
"""
    parts = [head]
    for i, body in enumerate(letters_bodies):
        parts.append(body)
        if i < len(letters_bodies) - 1:
            parts.append('<div class="letter-divider"></div>\n')
    parts.append("</body></html>")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Only render the first N letters (useful for a quick look).")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    template = load_template()

    # Pull rows
    with connect() as conn:
        rows = list(conn.execute(READY_SQL))
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print("No mail-ready letters in the database. Nothing to render.")
        return 0

    print(f"Rendering {len(rows)} letter(s)...")

    letters_bodies: list[str] = []
    css_block = ""
    skipped = 0

    for row in rows:
        rendered = render_one(template, dict(row))
        if rendered is None:
            skipped += 1
            continue
        if not css_block:
            css_block = extract_head(rendered)
        letters_bodies.append(extract_body(rendered))

    if not letters_bodies:
        print(f"All {skipped} letters had errors. Nothing rendered.")
        return 1

    combined_html = build_combined(letters_bodies, css_block)

    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    html_path = OUT_DIR / f"all_letters_{timestamp}.html"
    pdf_path  = OUT_DIR / f"all_letters_{timestamp}.pdf"
    html_path.write_text(combined_html, encoding="utf-8")
    print(f"Wrote combined HTML: {html_path} ({len(combined_html):,} bytes)")

    print("Rendering to PDF via Chromium...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.pdf(
            path=str(pdf_path),
            format="Letter",
            margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
            print_background=True,
            prefer_css_page_size=True,
        )
        browser.close()

    print(f"PDF ready: {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
    print(f"Letters: {len(letters_bodies)} rendered, {skipped} skipped (missing data)")
    print(f"Pages:  each letter is 2 pages (EN + ES), so expect ~{len(letters_bodies) * 2} pages total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
