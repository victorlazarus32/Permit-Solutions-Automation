"""
Render assets/permit_solutions_logo.svg to a high-resolution PNG for use in
the letter template (which embeds the PNG as a data URI) and the web app.

Output:
  assets/permit_solutions_logo.png        (1500x1500, used in mailed letters)
  app/static/permit_solutions_logo.png    (mirror — used in dashboard top bar)

Run after editing the SVG:
    python -m scripts.render_logo_png
"""
from __future__ import annotations

import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SVG_PATH        = PROJECT_ROOT / "assets" / "permit_solutions_logo.svg"
PNG_PATH        = PROJECT_ROOT / "assets" / "permit_solutions_logo.png"
APP_PNG_PATH    = PROJECT_ROOT / "app" / "static" / "permit_solutions_logo.png"

# 1500x1500 keeps the logo crisp at the largest size we use it (0.95in tall in
# the letter, which is ~285px at 300 DPI). 1500 gives 5x margin so it scales
# down without aliasing.
RENDER_PX = 1500


def main() -> int:
    if not SVG_PATH.exists():
        raise SystemExit(f"SVG not found: {SVG_PATH}")
    svg_text = SVG_PATH.read_text(encoding="utf-8")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; background: white; }}
  .frame {{ width: {RENDER_PX}px; height: {RENDER_PX}px;
            display: flex; align-items: center; justify-content: center; }}
  svg    {{ width: 100%; height: 100%; display: block; }}
</style></head>
<body><div class="frame">{svg_text}</div></body></html>
"""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": RENDER_PX, "height": RENDER_PX},
            device_scale_factor=1,
        )
        page = ctx.new_page()
        page.set_content(html, wait_until="networkidle")
        # Screenshot the .frame element only — exact bounds, no extra padding
        elem = page.locator(".frame")
        elem.screenshot(path=str(PNG_PATH), omit_background=False)
        browser.close()

    shutil.copyfile(PNG_PATH, APP_PNG_PATH)

    print(f"Rendered {SVG_PATH.name} -> {PNG_PATH.relative_to(PROJECT_ROOT)} "
          f"({PNG_PATH.stat().st_size:,} bytes, {RENDER_PX}x{RENDER_PX})")
    print(f"Mirrored to {APP_PNG_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
