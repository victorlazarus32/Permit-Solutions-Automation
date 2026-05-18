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
#  - --with-deps would apt-install missing system libraries; Render's Python
#    base image already has the libs Chromium needs, so plain `playwright
#    install chromium` is enough and avoids needing sudo at build time.
#  - Chromium is ~150 MB. First build is slower; subsequent builds reuse
#    Render's build cache.

set -euo pipefail

echo "==> pip install"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> playwright install chromium"
python -m playwright install chromium

echo "==> build complete"
