"""
Flask receiver for Lob webhooks.

Run locally:
    pip install flask
    export LOB_WEBHOOK_SECRET=whsec_xxx   # from Lob dashboard → Webhooks
    python -m webhook.server

Then expose via ngrok (or deploy behind a real domain):
    ngrok http 5000

In the Lob dashboard, add a webhook endpoint pointing to:
    https://<your-ngrok-or-domain>/lob-webhook
and subscribe to letter events (created, mailed, in_transit, in_local_area,
processed_for_delivery, re_routed, returned_to_sender, delivered).

Production notes:
  - Lob retries on non-2xx responses, so always return 200 if the request is
    valid — even if the event refers to an unknown letter.
  - Use a real WSGI server (gunicorn) in production, not Flask's dev server.
  - This module never logs the webhook secret.
"""
from __future__ import annotations

import json
import logging
import os
import sys

try:
    from flask import Flask, request, jsonify
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Flask is required for the webhook server.\n"
        "Install with:  pip install flask\n"
    )
    raise

from db import init_db
from webhook.handler import (
    handle_event,
    verify_signature,
    SignatureError,
)

log = logging.getLogger("webhook.server")

app = Flask(__name__)


@app.route("/lob-webhook", methods=["POST"])
def lob_webhook():
    secret = os.environ.get("LOB_WEBHOOK_SECRET", "").strip()
    raw_body = request.get_data()  # raw bytes — required for signature verification

    # Verify signature unless explicitly disabled (only for local dev!)
    if os.environ.get("LOB_WEBHOOK_SKIP_VERIFICATION") == "1":
        log.warning("LOB_WEBHOOK_SKIP_VERIFICATION=1 — skipping signature check (dev only)")
    else:
        try:
            verify_signature(
                raw_body=raw_body,
                signature_header=request.headers.get("Lob-Signature"),
                timestamp_header=request.headers.get("Lob-Signature-Timestamp"),
                secret=secret,
            )
        except SignatureError as e:
            log.warning("Rejected webhook: %s", e)
            # Lob expects 400 (not 401) for signature failures so they don't retry forever.
            return jsonify({"error": "signature verification failed"}), 400

    # Parse JSON body
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as e:
        log.warning("Bad JSON body: %s", e)
        return jsonify({"error": "invalid json"}), 400

    # Hand off to the pure handler. We intentionally swallow exceptions here
    # and return 200 so Lob doesn't keep retrying on a bug — the handler
    # logs everything for our own debugging.
    try:
        result = handle_event(event)
    except Exception as e:
        log.exception("Handler raised: %s", e)
        return jsonify({"error": "handler error"}), 200

    return jsonify(result), 200


@app.route("/healthz", methods=["GET"])
def healthz():
    """Liveness probe for load balancers / uptime checks."""
    return jsonify({"ok": True}), 200


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(message)s",
    )
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    log.info("Starting Lob webhook receiver on 0.0.0.0:%d", port)
    if os.environ.get("LOB_WEBHOOK_SKIP_VERIFICATION") == "1":
        log.warning("Signature verification is DISABLED — never do this in production.")
    elif not os.environ.get("LOB_WEBHOOK_SECRET"):
        log.warning(
            "LOB_WEBHOOK_SECRET is not set — incoming requests will be rejected. "
            "Set the env var or use LOB_WEBHOOK_SKIP_VERIFICATION=1 for local testing."
        )
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
