"""
Lob webhook receiver — keeps the DB in sync with the real-world status of
each letter we sent.

How it fits together:
  1. We POST a letter to Lob → Lob returns a letter ID (ltr_xxx). We save
     that ID on the violations row (lob_letter_id).
  2. As the letter moves through Lob → printer → USPS → recipient, Lob fires
     events: letter.printed, letter.mailed, letter.in_transit,
     letter.processed_for_delivery, letter.delivered, letter.returned_to_sender.
  3. Lob HTTP-POSTs each event to a URL we configure on the Lob dashboard.
  4. This module handles the POST: looks up the row by lob_letter_id and
     updates lob_status / lob_mailed_at / lob_delivered_at / lob_last_event_at.

Security: Lob signs each webhook POST with HMAC-SHA256 using a secret you
configure in the Lob dashboard. We verify the signature against the
LOB_WEBHOOK_SECRET env var. If the env var is unset (e.g. first deploy
before you've configured the secret), we log a warning and accept anyway —
flip that to strict by setting LOB_WEBHOOK_STRICT=1.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from db import DB_PATH

log = logging.getLogger("lob_webhook")

WEBHOOK_SECRET = os.environ.get("LOB_WEBHOOK_SECRET", "").strip()
STRICT = os.environ.get("LOB_WEBHOOK_STRICT", "").strip().lower() in ("1", "true", "yes")

# Map Lob event types to (canonical_status, timestamp_column_to_set_or_None).
# Anything not in this map still gets the canonical lob_status updated.
EVENT_MAP: dict[str, tuple[str, str | None]] = {
    # letter lifecycle (in roughly the order they fire)
    "letter.created":                  ("submitted",                None),
    "letter.rendered_pdf":             ("rendered",                 None),
    "letter.processed_for_delivery":   ("processed_for_delivery",   None),
    "letter.mailed":                   ("mailed",                   "lob_mailed_at"),
    "letter.in_transit":               ("in_transit",               "lob_mailed_at"),
    "letter.in_local_area":            ("in_local_area",            None),
    "letter.delivered":                ("delivered",                "lob_delivered_at"),
    "letter.re-routed":                ("re_routed",                None),
    "letter.returned_to_sender":       ("returned_to_sender",       None),
    "letter.failed":                   ("failed",                   None),

    # tracking events (sometimes named with the "tracking." prefix)
    "tracking.created":                ("submitted",                None),
    "tracking.mailed":                 ("mailed",                   "lob_mailed_at"),
    "tracking.in_transit":             ("in_transit",               "lob_mailed_at"),
    "tracking.in_local_area":          ("in_local_area",            None),
    "tracking.processed_for_delivery": ("processed_for_delivery",   None),
    "tracking.delivered":              ("delivered",                "lob_delivered_at"),
    "tracking.re-routed":              ("re_routed",                None),
    "tracking.returned_to_sender":     ("returned_to_sender",       None),
}


# ---------- signature verification ----------

def verify_signature(*, body: bytes, signature_header: str | None,
                     timestamp_header: str | None) -> bool:
    """
    Verify Lob's HMAC-SHA256 signature. The header value Lob sends is the
    hex digest of: HMAC(secret, timestamp + '.' + raw_body, SHA256).

    Returns True if the signature matches OR if no secret is configured AND
    we're not in strict mode (development convenience).
    """
    if not WEBHOOK_SECRET:
        if STRICT:
            log.error("LOB_WEBHOOK_SECRET not set and STRICT mode on; rejecting")
            return False
        log.warning("LOB_WEBHOOK_SECRET not set; accepting webhook without "
                    "signature verification. Set the env var to enforce.")
        return True

    if not signature_header or not timestamp_header:
        log.warning("Missing Lob-Signature or Lob-Signature-Timestamp header")
        return False

    payload = f"{timestamp_header}.".encode("utf-8") + body
    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


# ---------- event handling ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def handle_event(event: dict) -> dict:
    """
    Apply one Lob event to the DB. `event` is the parsed JSON body Lob sent.

    Returns a small summary dict for logging/debugging:
        {ok, letter_id, event_type, status, matched, message?}
    """
    event_type = (event.get("event_type") or {}).get("id") \
                 if isinstance(event.get("event_type"), dict) \
                 else event.get("event_type")
    body = event.get("body") or {}
    # body.id is the letter id; some payloads put it at top-level too
    letter_id = body.get("id") or event.get("letter_id") or body.get("letter_id")

    if not letter_id:
        log.warning("Event missing letter id: type=%s payload_keys=%s",
                    event_type, list(event.keys()))
        return {"ok": False, "message": "missing letter id"}

    mapped = EVENT_MAP.get(event_type, (event_type or "unknown", None))
    canonical_status, timestamp_col = mapped

    now = _now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.execute(
            "SELECT source, case_number FROM violations WHERE lob_letter_id = ?",
            (letter_id,),
        ).fetchone()
        if not before:
            log.warning("No violation row for letter_id=%s (event %s)",
                        letter_id, event_type)
            return {"ok": False, "letter_id": letter_id,
                    "event_type": event_type, "matched": False,
                    "message": "no matching row"}

        cols = ["lob_status = ?", "lob_last_event_at = ?"]
        params: list = [canonical_status, now]
        if timestamp_col:
            # Only set the timestamp the first time we see this transition.
            cols.append(f"{timestamp_col} = COALESCE({timestamp_col}, ?)")
            params.append(now)
        params.extend([letter_id])

        conn.execute(
            f"UPDATE violations SET {', '.join(cols)} WHERE lob_letter_id = ?",
            params,
        )

    log.info("event %s -> %s for letter %s (%s/%s)",
             event_type, canonical_status, letter_id,
             before["source"], before["case_number"])
    return {
        "ok":          True,
        "letter_id":   letter_id,
        "event_type":  event_type,
        "status":      canonical_status,
        "matched":     True,
    }


def handle_request(*, raw_body: bytes, signature: str | None,
                   timestamp: str | None) -> tuple[int, dict]:
    """
    Top-level entry point used by the Flask route.
    Returns (http_status_code, response_body_dict).
    """
    if not verify_signature(body=raw_body, signature_header=signature,
                            timestamp_header=timestamp):
        return 401, {"ok": False, "error": "signature verification failed"}

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("Could not parse webhook body: %s", e)
        return 400, {"ok": False, "error": "invalid json"}

    summary = handle_event(event)
    return 200, summary
