"""
One-time helper to upload the violation letter template to Lob.

Usage:
    export LOB_API_KEY=test_xxx
    python -m scripts.upload_template

Prints a `tmpl_xxx` ID. Paste it into your .env as LOB_TEMPLATE_ID.

Subsequent edits to the template can be made directly in the Lob dashboard
(Templates section) without re-running this script — Lob keeps version history
automatically.

Re-run this script ONLY if you want a brand-new template (e.g. Spanish version).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = PROJECT_ROOT / "templates" / "violation_letter_en.html"

LOB_API_BASE = "https://api.lob.com/v1"


def basic_auth_header(api_key: str) -> str:
    raw = f"{api_key}:".encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def upload(template_path: Path, description: str, api_key: str) -> dict:
    html = template_path.read_text(encoding="utf-8")
    body = json.dumps({
        "description": description,
        "html":        html,
        "engine":      "legacy",   # default; switch to "handlebars" if you need conditionals
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LOB_API_BASE}/templates",
        data=body,
        method="POST",
        headers={
            "Authorization": basic_auth_header(api_key),
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"Lob HTTP {e.code}\n{e.read().decode('utf-8', 'replace')}\n")
        raise


def main() -> int:
    p = argparse.ArgumentParser(description="Upload a Lob HTML template.")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--description", default="Permit Solutions — Violation Notice (English)")
    args = p.parse_args()

    api_key = os.environ.get("LOB_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("ERROR: LOB_API_KEY env var is required.\n")
        return 1
    if not args.template.exists():
        sys.stderr.write(f"ERROR: template not found: {args.template}\n")
        return 1

    print(f"Uploading {args.template} → Lob …")
    resp = upload(args.template, args.description, api_key)
    print()
    print(f"✓ Uploaded.")
    print(f"  template ID:  {resp.get('id')}")
    print(f"  description:  {resp.get('description')}")
    print(f"  versions:     {resp.get('versions') or 'see dashboard'}")
    print()
    print("Add this line to your .env file:")
    print(f"  LOB_TEMPLATE_ID={resp.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
