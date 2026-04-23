"""
Webhook event handling — pure logic, no HTTP framework.

This module is separated from server.py so it can be unit-tested without
spinning up Flask. The server is just a thin wrapper that calls these
functions with the raw request body and headers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from db import connect

log = logging.getLogger("webhook")

# Lob event types we care about for letters. Anything else is logged and ignored.
# Mapped to a normalized status string we store in the lob_status column.
LETTER_EVENT_TO_STATUS = {
    "letter.created":                "created",
    "letter.rendered_pdf":           "rendered",
    "letter.mailed":                 "mailed",
    "letter.in_transit":             "in_transit",
    "letter.in_local_area":          "in_local_area",
    "letter.processed_for_delivery": "processed_for_delivery",
    "letter.re_routed":              "re_routed",
    "letter.returned_to_sender":     "returned_to_sender",
    "letter.delivered":              "delivered",
    "letter.failed":                 "failed",
}

# Timestamp tolerance in seconds — reject events whose Lob-Signature-Timestamp
# is older than this. Defends against replay attacks.
TIMESTAMP_TOLERANCE_SECONDS = 300  # 5 minutes, per Lob's recommendation


# ---------- Signature verification ----------

class SignatureError(Exception):
    """Raised when a webhook fails signature verification."""


def verify_signature(
    *,
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    secret: str,
    now: float | None = None,
) -> None:
    """
    Verify the Lob-Signature header. Raises SignatureError if invalid.

    Per Lob: HMAC-SHA256 over "<timestamp>.<raw_body>" using the webhook secret.
    """
    if not signature_header or not timestamp_header:
        raise SignatureError("missing Lob-Signature or Lob-Signature-Timestamp header")
    if not secret:
        raise SignatureError("LOB_WEBHOOK_SECRET is not configured on this server")

    # Replay-attack defense: reject stale timestamps
    try:
        ts = int(timestamp_header)
    except ValueError as e:
        raise SignatureError(f"timestamp header is not an integer: {timestamp_header!r}") from e
    now = now if now is not None else time.time()
    if abs(now - ts) > TIMESTAMP_TOLERANCE_SECONDS:
        raise SignatureError(
            f"timestamp outside tolerance ({abs(now-ts)}s > {TIMESTAMP_TOLERANCE_SECONDS}s)"
        )

    # Compute expected signature
    signed_payload = timestamp_header.encode("ascii") + b"." + raw_body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison
    if not hmac.compare_digest(expected, signature_header):
        raise SignatureError("signature mismatch")


# ---------- Event handling ----------

def handle_event(event: dict[str, Any]) -> dict[str, Any]:
    """
    Process a Lob event payload and update the DB if it's a letter event we
    track. Returns a small dict describing what happened (for logging/testing).

    Idempotent: replaying the same event yields the same DB state.
    """
    event_type = event.get("event_type", {}).get("id") or event.get("type") or ""
    body = event.get("body", {}) or {}

    # Only handle letter events
    if not event_type.startswith("letter."):
        log.info("Ignoring non-letter event_type=%r", event_type)
        return {"action": "ignored", "reason": "non-letter event"}

    status = LETTER_EVENT_TO_STATUS.get(event_type)
    if not status:
        log.info("Unknown letter event_type=%r", event_type)
        return {"action": "ignored", "reason": f"unknown letter event {event_type}"}

    letter_id = body.get("id") or ""
    metadata = body.get("metadata") or {}
    source = metadata.get("source")
    case_number = metadata.get("case_number")

    # The metadata route is preferred (one DB lookup) — fall back to letter_id
    # if metadata is missing (older letters created before metadata was set).
    if source and case_number:
        rows_updated = _update_by_metadata(
            source=source,
            case_number=case_number,
            letter_id=letter_id,
            status=status,
            event_type=event_type,
            body=body,
        )
        lookup = "metadata"
    elif letter_id:
        rows_updated = _update_by_letter_id(
            letter_id=letter_id,
            status=status,
            event_type=event_type,
            body=body,
        )
        lookup = "letter_id"
    else:
        log.warning("Event has no metadata and no body.id — cannot route: %r", event)
        return {"action": "dropped", "reason": "no routing info"}

    if rows_updated == 0:
        log.warning(
            "No DB row matched for event_type=%s letter_id=%s source=%s case=%s",
            event_type, letter_id, source, case_number,
        )
        return {
            "action":     "no_match",
            "event_type": event_type,
            "letter_id":  letter_id,
            "lookup":     lookup,
        }

    log.info(
        "Updated %d row(s): event_type=%s status=%s letter_id=%s",
        rows_updated, event_type, status, letter_id,
    )
    return {
        "action":     "updated",
        "event_type": event_type,
        "status":     status,
        "letter_id":  letter_id,
        "rows":       rows_updated,
        "lookup":     lookup,
    }


def _update_by_metadata(
    *,
    source: str,
    case_number: str,
    letter_id: str,
    status: str,
    event_type: str,
    body: dict,
) -> int:
    """Find the row by (source, case_number) and update its lob_* columns."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mailed_at = _extract_mailed_at(event_type, body)
    delivered_at = _extract_delivered_at(event_type, body)

    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE violations
            SET lob_letter_id     = COALESCE(?, lob_letter_id),
                lob_status        = ?,
                lob_mailed_at     = COALESCE(lob_mailed_at, ?),
                lob_delivered_at  = COALESCE(lob_delivered_at, ?),
                lob_last_event_at = ?
            WHERE source = ? AND case_number = ?
            """,
            (letter_id or None, status, mailed_at, delivered_at, now_iso, source, case_number),
        )
        return cur.rowcount


def _update_by_letter_id(
    *,
    letter_id: str,
    status: str,
    event_type: str,
    body: dict,
) -> int:
    """Fallback: locate by letter_id when metadata isn't present."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mailed_at = _extract_mailed_at(event_type, body)
    delivered_at = _extract_delivered_at(event_type, body)

    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE violations
            SET lob_status        = ?,
                lob_mailed_at     = COALESCE(lob_mailed_at, ?),
                lob_delivered_at  = COALESCE(lob_delivered_at, ?),
                lob_last_event_at = ?
            WHERE lob_letter_id = ?
            """,
            (status, mailed_at, delivered_at, now_iso, letter_id),
        )
        return cur.rowcount


def _extract_mailed_at(event_type: str, body: dict) -> str | None:
    if event_type != "letter.mailed":
        return None
    # Lob includes the event time on the tracking_events array; take the latest.
    for ev in reversed(body.get("tracking_events", []) or []):
        if ev.get("name", "").lower() in ("mailed", "in_transit"):
            return ev.get("time") or ev.get("date_created")
    return body.get("date_modified") or body.get("date_created")


def _extract_delivered_at(event_type: str, body: dict) -> str | None:
    if event_type != "letter.delivered":
        return None
    for ev in reversed(body.get("tracking_events", []) or []):
        if ev.get("name", "").lower() == "delivered":
            return ev.get("time") or ev.get("date_created")
    return body.get("date_modified") or body.get("date_created")
