#!/usr/bin/env bash
# Render build script.
#
# Pip-installs the app's Python dependencies, then downloads the Chromium
# browser binary that Playwright needs for the Miami-Dade Unincorporated
# scraper (its connector drives a real browser to fetch the Excel export).
#
# Set Render's "Build Command" to:  ./build.sh
#
# Notes:
#  - Chromium is ~150 MB. First build is slower; later builds reuse cache.
#  - Verification step at the end fails the build if Chromium can't launch,
#    so we never end up with a "deploy succeeded" that's secretly broken.

set -euo pipefail

echo "==> [build.sh] starting"
echo "==> python: $(python --version 2>&1)"
echo "==> which python: $(which python)"

echo "==> pip install"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> playwright install chromium"
python -m playwright install chromium

echo "==> verifying Chromium can launch"
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop(); print('  Chromium launch: OK')"

echo "==> [build.sh] complete"
