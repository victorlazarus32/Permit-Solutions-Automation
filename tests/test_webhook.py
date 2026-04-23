"""
Tests for webhook.handler.

Run with:  python -m unittest tests.test_webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


# Point the DB module at a temp file BEFORE importing handler/db
_TMPDIR = tempfile.mkdtemp(prefix="permit_test_")
os.environ["PERMIT_TEST_DB_DIR"] = _TMPDIR

import db as db_module  # noqa: E402
db_module.DB_PATH = Path(_TMPDIR) / "test_violations.db"

from webhook.handler import (  # noqa: E402
    handle_event,
    verify_signature,
    SignatureError,
)


SECRET = "whsec_test_supersecret"


def _signed_headers(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    """Helper: build valid Lob-style signed headers for `body`."""
    if ts is None:
        ts = int(time.time())
    payload = f"{ts}.".encode("ascii") + body
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return {
        "Lob-Signature": sig,
        "Lob-Signature-Timestamp": str(ts),
    }


# ---------- Signature verification ----------

class TestSignature(unittest.TestCase):
    def test_valid_signature_passes(self):
        body = b'{"hello":"world"}'
        h = _signed_headers(body)
        verify_signature(
            raw_body=body,
            signature_header=h["Lob-Signature"],
            timestamp_header=h["Lob-Signature-Timestamp"],
            secret=SECRET,
        )  # no exception

    def test_tampered_body_fails(self):
        body = b'{"hello":"world"}'
        h = _signed_headers(body)
        with self.assertRaises(SignatureError):
            verify_signature(
                raw_body=b'{"hello":"WORLD"}',  # tampered
                signature_header=h["Lob-Signature"],
                timestamp_header=h["Lob-Signature-Timestamp"],
                secret=SECRET,
            )

    def test_wrong_secret_fails(self):
        body = b'{"x":1}'
        h = _signed_headers(body, secret="wrong_secret")
        with self.assertRaises(SignatureError):
            verify_signature(
                raw_body=body,
                signature_header=h["Lob-Signature"],
                timestamp_header=h["Lob-Signature-Timestamp"],
                secret=SECRET,
            )

    def test_stale_timestamp_fails(self):
        body = b'{"x":1}'
        old_ts = int(time.time()) - 3600  # 1 hour old
        h = _signed_headers(body, ts=old_ts)
        with self.assertRaises(SignatureError) as ctx:
            verify_signature(
                raw_body=body,
                signature_header=h["Lob-Signature"],
                timestamp_header=h["Lob-Signature-Timestamp"],
                secret=SECRET,
            )
        self.assertIn("tolerance", str(ctx.exception))

    def test_missing_headers_fails(self):
        with self.assertRaises(SignatureError):
            verify_signature(raw_body=b'{}', signature_header=None, timestamp_header="123", secret=SECRET)
        with self.assertRaises(SignatureError):
            verify_signature(raw_body=b'{}', signature_header="abc", timestamp_header=None, secret=SECRET)

    def test_missing_secret_fails(self):
        body = b'{}'
        h = _signed_headers(body)
        with self.assertRaises(SignatureError):
            verify_signature(
                raw_body=body,
                signature_header=h["Lob-Signature"],
                timestamp_header=h["Lob-Signature-Timestamp"],
                secret="",
            )


# ---------- Event handler (DB updates) ----------

class TestHandleEvent(unittest.TestCase):
    """End-to-end test of event → DB update."""

    def setUp(self):
        # Fresh DB per test
        if db_module.DB_PATH.exists():
            db_module.DB_PATH.unlink()
        db_module.init_db()

        # Seed one row that we'll update via webhooks
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with db_module.connect() as c:
            c.execute(
                """INSERT INTO violations
                   (source, case_number, owner_full_name, owner_mailing_address,
                    matched_keywords, first_seen_at, last_seen_at,
                    lob_letter_id, lob_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("miami_dade_unincorporated", "20260247184",
                 "MORITZ ESSER", "1769 NW 67TH ST , MIAMI FL 33147",
                 "fence", now, now,
                 "ltr_existing_id", "created"),
            )

    def _row(self) -> sqlite3.Row:
        with db_module.connect() as c:
            return c.execute(
                "SELECT * FROM violations WHERE source=? AND case_number=?",
                ("miami_dade_unincorporated", "20260247184"),
            ).fetchone()

    def test_letter_mailed_event_via_metadata(self):
        event = {
            "event_type": {"id": "letter.mailed"},
            "body": {
                "id": "ltr_abc123",
                "metadata": {
                    "source": "miami_dade_unincorporated",
                    "case_number": "20260247184",
                },
                "date_modified": "2026-04-25T10:00:00.000Z",
                "tracking_events": [
                    {"name": "Mailed", "time": "2026-04-25T10:00:00.000Z"},
                ],
            },
        }
        result = handle_event(event)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["status"], "mailed")
        self.assertEqual(result["lookup"], "metadata")
        self.assertEqual(result["rows"], 1)

        row = self._row()
        self.assertEqual(row["lob_status"], "mailed")
        self.assertEqual(row["lob_mailed_at"], "2026-04-25T10:00:00.000Z")
        self.assertIsNotNone(row["lob_last_event_at"])

    def test_letter_delivered_event_sets_delivered_at(self):
        event = {
            "event_type": {"id": "letter.delivered"},
            "body": {
                "id": "ltr_abc123",
                "metadata": {
                    "source": "miami_dade_unincorporated",
                    "case_number": "20260247184",
                },
                "tracking_events": [
                    {"name": "Delivered", "time": "2026-04-30T15:30:00.000Z"},
                ],
            },
        }
        result = handle_event(event)
        self.assertEqual(result["status"], "delivered")
        row = self._row()
        self.assertEqual(row["lob_status"], "delivered")
        self.assertEqual(row["lob_delivered_at"], "2026-04-30T15:30:00.000Z")

    def test_returned_to_sender_event(self):
        event = {
            "event_type": {"id": "letter.returned_to_sender"},
            "body": {
                "id": "ltr_abc123",
                "metadata": {
                    "source": "miami_dade_unincorporated",
                    "case_number": "20260247184",
                },
            },
        }
        result = handle_event(event)
        self.assertEqual(result["status"], "returned_to_sender")
        self.assertEqual(self._row()["lob_status"], "returned_to_sender")

    def test_fallback_to_letter_id_when_metadata_missing(self):
        # Update the seeded row's letter ID to match the event
        with db_module.connect() as c:
            c.execute(
                "UPDATE violations SET lob_letter_id=? WHERE case_number=?",
                ("ltr_lookup_test", "20260247184"),
            )
        event = {
            "event_type": {"id": "letter.in_transit"},
            "body": {
                "id": "ltr_lookup_test",
                # NO metadata
            },
        }
        result = handle_event(event)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["lookup"], "letter_id")
        self.assertEqual(self._row()["lob_status"], "in_transit")

    def test_no_match_returns_no_match(self):
        event = {
            "event_type": {"id": "letter.delivered"},
            "body": {
                "id": "ltr_nonexistent",
                "metadata": {
                    "source": "some_other_source",
                    "case_number": "DOES_NOT_EXIST",
                },
            },
        }
        result = handle_event(event)
        self.assertEqual(result["action"], "no_match")

    def test_non_letter_event_ignored(self):
        event = {
            "event_type": {"id": "postcard.delivered"},
            "body": {"id": "psc_xyz", "metadata": {}},
        }
        result = handle_event(event)
        self.assertEqual(result["action"], "ignored")

    def test_idempotent_replay(self):
        """Replaying the same event shouldn't change the DB further."""
        event = {
            "event_type": {"id": "letter.delivered"},
            "body": {
                "id": "ltr_abc123",
                "metadata": {
                    "source": "miami_dade_unincorporated",
                    "case_number": "20260247184",
                },
                "tracking_events": [
                    {"name": "Delivered", "time": "2026-04-30T15:30:00.000Z"},
                ],
            },
        }
        handle_event(event)
        first = dict(self._row())
        time.sleep(0.01)  # ensure timestamp would differ if it changed
        handle_event(event)  # replay
        second = dict(self._row())

        # delivered_at should be unchanged (COALESCE preserves first value)
        self.assertEqual(first["lob_delivered_at"], second["lob_delivered_at"])
        # status is the same
        self.assertEqual(first["lob_status"], second["lob_status"])


if __name__ == "__main__":
    unittest.main()
