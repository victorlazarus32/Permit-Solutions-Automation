"""
Daily automated pipeline run — designed to be invoked by Render Cron Jobs.

What it does (in order):
    1. Pull City of Homestead violations via the Tyler EnerGov API.
    2. Pull Miami-Dade Unincorporated violations via the Selenium scraper.
       (Requires Chromium — build.sh installs it for this service.)
    3. Compose a summary of what got pulled + what's now letter-ready.
    4. Optionally SEND the new letters via Lob.
        - Controlled by env var DAILY_AUTO_SEND=1 (default OFF for safety).
        - Send window controlled by DAILY_SEND_SINCE_DAYS (default 2).
    5. Write the summary to the daily_runs table.
    6. (Future) Email the summary to operators when SendGrid is wired up.

Failure handling: each step is wrapped — if Homestead pull blows up, we
still try Miami-Dade and still record what we have. We never silently
swallow a failure though; the row's status field tells the operator
whether everything completed.

Render Cron Job setup:
    Service Type:     Cron Job
    Repository:       same (victorlazarus32/Permit-Solutions-Automation)
    Branch:           main
    Build Command:    ./build.sh  (same as web service — pip + chromium)
    Schedule (UTC):   0 11 * * *   (= 7 AM EDT)
    Start Command:    python -m scripts.daily_run
    Env vars:         inherit from web service group (DB_PATH, DATA_DIR,
                      USERS_FILE, LOB_API_KEY, ...), plus optional
                      DAILY_AUTO_SEND=1 and DAILY_SEND_SINCE_DAYS=2.

Local run (for testing):
    python -m scripts.daily_run
    # Or just one piece:
    python -m scripts.daily_run --homestead-only
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone

from db import DB_PATH, init_db

log = logging.getLogger("daily_run")


# ---------- helpers ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _record_started() -> int:
    """Insert a fresh daily_runs row with status='running'. Return its id."""
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO daily_runs (started_at, status, auto_send_enabled) "
            "VALUES (?, 'running', ?)",
            (_now(), 1 if _env_truthy("DAILY_AUTO_SEND") else 0),
        )
        return cur.lastrowid


def _update_row(run_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    params = dict(fields)
    params["id"] = run_id
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE daily_runs SET {sets} WHERE id = :id", params)


# ---------- pipeline steps ----------

def _pull_homestead() -> dict:
    """Pull Tyler / Homestead. Returns {pulled, in_scope, inserted, error}."""
    out = {"pulled": 0, "in_scope": 0, "inserted": 0, "error": None}
    try:
        from connectors.tyler_energov import run as _run_tyler
        summary = _run_tyler("homestead")
        out["pulled"]   = summary.get("fetched")  or 0
        out["in_scope"] = summary.get("in_scope") or 0
        out["inserted"] = summary.get("inserted") or 0
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.exception("Homestead pull failed")
    return out


def _pull_miami_dade() -> dict:
    """Pull Miami-Dade via Selenium. Returns {pulled, in_scope, inserted, error}."""
    out = {"pulled": 0, "in_scope": 0, "inserted": 0, "error": None}
    try:
        from connectors.miami_dade_unincorporated import run as _run_md
        summary = _run_md()  # defaults to "since last run"
        out["pulled"]   = summary.get("fetched")  or summary.get("matched_rows") or 0
        out["in_scope"] = summary.get("in_scope") or summary.get("matched_rows") or 0
        out["inserted"] = summary.get("inserted") or 0
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.exception("Miami-Dade pull failed")
    return out


def _pull_pinecrest() -> dict:
    """Pull Pinecrest via eTRAKiT (Playwright). Returns {pulled, in_scope, inserted, error}."""
    out = {"pulled": 0, "in_scope": 0, "inserted": 0, "error": None}
    try:
        from connectors.etrakit import run as _run_etrakit
        summary = _run_etrakit("pinecrest")
        out["pulled"]   = summary.get("fetched")  or 0
        out["in_scope"] = summary.get("in_scope") or 0
        out["inserted"] = summary.get("inserted") or 0
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.exception("Pinecrest pull failed")
    return out


def _send_letters(since_days: int) -> dict:
    """Send any letter-ready cases opened in the last N days via Lob."""
    out = {"considered": 0, "sent": 0, "skipped": 0, "failed": 0, "error": None}
    try:
        from lob_sender.send import send_batch
        summary = send_batch(limit=None, dry_run=False, since_days=since_days)
        out["considered"] = summary.get("considered", 0)
        out["sent"]       = summary.get("sent", 0)
        out["skipped"]    = summary.get("skipped", 0)
        out["failed"]     = summary.get("failed", 0)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.exception("Lob send failed")
    return out


# ---------- summary ----------

def _compose_summary(*, hs: dict, md: dict, pc: dict, send: dict | None,
                     auto_send: bool, since_days: int) -> str:
    """Plain-text report — looks like the email body we'll eventually send."""
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append(f"Daily Run — {today}")
    lines.append("=" * 50)
    lines.append("")
    lines.append("HOMESTEAD (Tyler API)")
    if hs["error"] == "skipped":
        lines.append("  Skipped this run.")
    elif hs["error"]:
        lines.append(f"  FAILED: {hs['error']}")
    else:
        lines.append(f"  Pulled:    {hs['pulled']:>3} raw rows")
        lines.append(f"  In scope:  {hs['in_scope']:>3} (after keyword filter)")
        lines.append(f"  Inserted:  {hs['inserted']:>3} new rows in DB")
    lines.append("")
    lines.append("MIAMI-DADE (Selenium scraper)")
    if md["error"] == "skipped":
        lines.append("  Skipped this run.")
    elif md["error"]:
        lines.append(f"  FAILED: {md['error']}")
    else:
        lines.append(f"  Pulled:    {md['pulled']:>3} raw rows")
        lines.append(f"  In scope:  {md['in_scope']:>3} (after keyword filter)")
        lines.append(f"  Inserted:  {md['inserted']:>3} new rows in DB")
    lines.append("")
    lines.append("PINECREST (eTRAKiT)")
    if pc["error"] == "skipped":
        lines.append("  Skipped this run.")
    elif pc["error"]:
        lines.append(f"  FAILED: {pc['error']}")
    else:
        lines.append(f"  Pulled:    {pc['pulled']:>3} raw rows")
        lines.append(f"  In scope:  {pc['in_scope']:>3} (after keyword filter)")
        lines.append(f"  Inserted:  {pc['inserted']:>3} new rows in DB")
    lines.append("")
    lines.append("LETTERS")
    if not auto_send:
        lines.append(f"  Auto-send is OFF. Review at https://app.permitsolutions.us/queue")
        lines.append(f"  and click Mail Today's Letters when ready.")
    elif send is None:
        lines.append("  Send was attempted but no summary returned.")
    elif send["error"]:
        lines.append(f"  Send FAILED: {send['error']}")
    else:
        lines.append(f"  Considered:  {send['considered']:>3} (open_date in last {since_days} day(s))")
        lines.append(f"  Sent:        {send['sent']:>3}")
        lines.append(f"  Skipped:     {send['skipped']:>3}")
        lines.append(f"  Failed:      {send['failed']:>3}")
    lines.append("")
    lines.append("Dashboard: https://app.permitsolutions.us/")
    return "\n".join(lines)


# ---------- main ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Daily Permit Solutions automated run.")
    p.add_argument("--homestead-only", action="store_true",
                   help="Skip Miami-Dade + Pinecrest (useful when one is broken).")
    p.add_argument("--miami-dade-only", action="store_true",
                   help="Skip Homestead + Pinecrest (useful for retrying MD only).")
    p.add_argument("--pinecrest-only", action="store_true",
                   help="Skip Homestead + Miami-Dade (useful for retrying Pinecrest only).")
    p.add_argument("--dry-run", action="store_true",
                   help="Pull, but do not send letters even if DAILY_AUTO_SEND=1.")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    auto_send = _env_truthy("DAILY_AUTO_SEND") and not args.dry_run
    since_days = int(os.environ.get("DAILY_SEND_SINCE_DAYS") or "2")

    run_id = _record_started()
    log.info("Daily run %d started. auto_send=%s since_days=%s",
             run_id, auto_send, since_days)

    overall_status = "success"

    # 1. Homestead
    hs = {"pulled": 0, "in_scope": 0, "inserted": 0, "error": "skipped"}
    if not (args.miami_dade_only or args.pinecrest_only):
        hs = _pull_homestead()
    if hs["error"] and hs["error"] != "skipped":
        overall_status = "partial"

    # 2. Miami-Dade
    md = {"pulled": 0, "in_scope": 0, "inserted": 0, "error": "skipped"}
    if not (args.homestead_only or args.pinecrest_only):
        md = _pull_miami_dade()
    if md["error"] and md["error"] != "skipped":
        overall_status = "partial"

    # 3. Pinecrest (via eTRAKiT — same Playwright stack as Miami-Dade)
    pc = {"pulled": 0, "in_scope": 0, "inserted": 0, "error": "skipped"}
    if not (args.homestead_only or args.miami_dade_only):
        pc = _pull_pinecrest()
    if pc["error"] and pc["error"] != "skipped":
        overall_status = "partial"

    # 4. Optional auto-send
    send: dict | None = None
    if auto_send:
        send = _send_letters(since_days=since_days)
        if send.get("error") or (send.get("failed") or 0) > 0:
            overall_status = "partial" if overall_status == "success" else overall_status

    # 5. Compose summary + record
    summary = _compose_summary(hs=hs, md=md, pc=pc, send=send,
                               auto_send=auto_send, since_days=since_days)
    log.info("Summary:\n%s", summary)

    _update_row(
        run_id,
        finished_at=_now(),
        status=overall_status,
        homestead_pulled=hs["pulled"],
        homestead_in_scope=hs["in_scope"],
        homestead_inserted=hs["inserted"],
        md_pulled=md["pulled"],
        md_in_scope=md["in_scope"],
        md_inserted=md["inserted"],
        pinecrest_pulled=pc["pulled"],
        pinecrest_in_scope=pc["in_scope"],
        pinecrest_inserted=pc["inserted"],
        letters_eligible=(send or {}).get("considered", 0),
        letters_sent=(send or {}).get("sent", 0),
        letters_skipped=(send or {}).get("skipped", 0),
        letters_failed=(send or {}).get("failed", 0),
        error_text=next(
            (e for e in (hs["error"], md["error"], pc["error"], (send or {}).get("error"))
             if e and e != "skipped"),
            None,
        ),
        summary_text=summary,
    )

    # 5. Future: email the summary via SendGrid when API key is set.
    # Stubbed here so the contract is documented; uncomment when ready:
    #
    #     if os.environ.get("SENDGRID_API_KEY"):
    #         from emailer import send_daily_report
    #         send_daily_report(summary)
    #
    # For now, the summary is viewable on /reports.

    log.info("Daily run %d complete. status=%s", run_id, overall_status)
    return 0 if overall_status == "success" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Make sure unhandled exceptions actually show up in Render logs.
        traceback.print_exc()
        sys.exit(2)
