"""
Lob US address verification — pre-flight check before paying ~$1.50 per letter.

Lob's US Verifications API (https://api.lob.com/v1/us_verifications) costs about
$0.0075 per call, so verifying every address before mailing is a 200x ROI vs a
single returned/undeliverable letter.

What it does:
  - Sends the parsed address (line1/line2/city/state/zip) to Lob.
  - Lob returns a `deliverability` enum and a corrected/normalized version.
  - We return a small dict the sender can use to either skip the row,
    auto-correct it, or pass it through.

Deliverability values we care about:
  deliverable                      → safe to mail (use returned components — they may be corrected)
  deliverable_unnecessary_unit     → safe to mail; we sent an apt number that USPS doesn't track
  deliverable_incorrect_unit       → mail it; warn — unit number was wrong
  deliverable_missing_unit         → mail it; warn — building has units but we didn't supply one
  undeliverable                    → DO NOT MAIL — USPS would return it
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

LOB_API_BASE = "https://api.lob.com/v1"

log = logging.getLogger("lob_sender.verify")

# Statuses we'll mail on. Anything else = skip.
_MAILABLE_STATUSES = {
    "deliverable",
    "deliverable_unnecessary_unit",
    "deliverable_incorrect_unit",
    "deliverable_missing_unit",
}


def _basic_auth_header(api_key: str) -> str:
    raw = f"{api_key}:".encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def verify_us_address(addr: dict, api_key: str | None = None) -> dict:
    """
    Verify a US address with Lob.

    Args:
      addr: dict with keys address_line1, address_line2 (optional), address_city,
            address_state, address_zip. Extra keys like 'name' are ignored.
      api_key: Lob API key. Falls back to env LOB_API_KEY.

    Returns:
      {
        "ok":              bool,            # True iff Lob says mailable
        "deliverability":  str,             # raw Lob status
        "corrected":       dict | None,     # cleaned address_line1/.../zip if ok, else None
        "error":           str | None,      # populated when the API call itself fails
      }

    Never raises — failures are surfaced via the `error` key so callers can
    decide whether to skip the row or mail anyway.
    """
    api_key = (api_key or os.environ.get("LOB_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "deliverability": None, "corrected": None,
                "error": "missing_api_key"}

    body_obj: dict[str, Any] = {
        "primary_line":   addr.get("address_line1", ""),
        "city":           addr.get("address_city", ""),
        "state":          addr.get("address_state", ""),
        "zip_code":       addr.get("address_zip", ""),
    }
    if addr.get("address_line2"):
        body_obj["secondary_line"] = addr["address_line2"]

    body = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(
        f"{LOB_API_BASE}/us_verifications",
        data=body,
        method="POST",
        headers={
            "Authorization": _basic_auth_header(api_key),
            "Content-Type":  "application/json",
            "User-Agent":    "permit-solutions/0.1",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = "<no body>"
        log.warning("Lob verify HTTP %d: %s", e.code, err_body)
        return {"ok": False, "deliverability": None, "corrected": None,
                "error": f"http_{e.code}"}
    except Exception as e:
        log.warning("Lob verify failed: %s", e)
        return {"ok": False, "deliverability": None, "corrected": None,
                "error": f"{type(e).__name__}: {e}"[:200]}

    deliverability = (data.get("deliverability") or "").strip()
    ok = deliverability in _MAILABLE_STATUSES

    corrected = None
    if ok:
        # Lob returns the normalized address as top-level fields. Map them
        # back to the same shape our payload builder uses.
        corrected = {
            "address_line1": (data.get("primary_line") or addr.get("address_line1") or "").strip(),
            "address_line2": (data.get("secondary_line") or "").strip(),
            "address_city":  (data.get("components", {}).get("city") or addr.get("address_city") or "").strip().upper(),
            "address_state": (data.get("components", {}).get("state") or addr.get("address_state") or "").strip().upper(),
            "address_zip":   _format_zip(data.get("components", {})) or addr.get("address_zip", ""),
        }

    return {
        "ok":             ok,
        "deliverability": deliverability or None,
        "corrected":      corrected,
        "error":          None,
    }


def _format_zip(components: dict) -> str:
    """Prefer ZIP+4 from Lob when available, fall back to 5-digit."""
    five = components.get("zip_code") or ""
    plus4 = components.get("zip_code_plus_4") or ""
    if five and plus4:
        return f"{five}-{plus4}"
    return five
