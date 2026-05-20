#!/usr/bin/env bash
# Render build script.
#
# Pip-installs the app's Python dependencies, then downloads the Chromium
# browser binary that Playwright needs for the Miami-Dade Unincorporated
# scraper (its connector drives a real browser to fetch the Excel export).
#
# Set Render's "Build Command" to:  ./build.sh
#
# Critical: install browsers INSIDE the project source dir so they survive
# Render's build → runtime container handoff. The default install location
# (~/.cache/ms-playwright/) is OUTSIDE /opt/render/project/src/ and gets
# dropped between phases, leaving runtime with no browser binary.
#
# This script sets PLAYWRIGHT_BROWSERS_PATH for the build. You also need to
# set the same env var on Render's Environment page so runtime knows where
# to look. Suggested value: /opt/render/project/src/.playwright
#
# Verification step at the end fails the build if Chromium can't launch,
# so we never end up with a "deploy succeeded" that's secretly broken.

set -euo pipefail

# Install browsers into the project dir so they persist into runtime.
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/render/project/src/.playwright}"

echo "==> [build.sh] starting"
echo "==> python: $(python --version 2>&1)"
echo "==> which python: $(which python)"
echo "==> PLAYWRIGHT_BROWSERS_PATH: $PLAYWRIGHT_BROWSERS_PATH"

echo "==> pip install"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> playwright install chromium (-> $PLAYWRIGHT_BROWSERS_PATH)"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
python -m playwright install chromium

echo "==> verifying Chromium can launch from $PLAYWRIGHT_BROWSERS_PATH"
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=True); b.close(); p.stop(); print('  Chromium launch: OK')"

echo "==> [build.sh] complete"
