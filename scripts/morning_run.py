"""
Morning Run — the one script the employee runs at the start of the day.

Does three things in order:
  1. Pull new Miami-Dade Unincorporated violations since the last run.
  2. Process any Homestead PRR spreadsheets dropped in inbox/homestead/.
  3. Show today's numbers and, after typed confirmation (YES), send letters.

Dummy-proof guards:
  - Lock file in data/.morning_run.lock prevents accidental double-clicks.
  - Every error is caught and shown as plain English, not a Python traceback.
  - Mail step requires typing YES in full. Anything else = do not mail.
  - Every run appends to data/morning_run.log for later debugging.
  - Exit code is 0 on clean runs and "nothing-to-do" runs; 1 only on hard errors.
"""
from __future__ import annotations

import datetime as dt
import logging
import logging.handlers
import os
import sqlite3
import sys
import traceback
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env so LOB_API_KEY, LOB_FROM_ADDRESS_ID, etc. are available to the
# send.py module without the employee having to set env vars by hand.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# Windows consoles default to cp1252 which chokes on many Unicode chars.
# Force stdout/stderr to UTF-8 so our log lines and friendly messages render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

LOCK_FILE = PROJECT_ROOT / "data" / ".morning_run.lock"
LOG_FILE  = PROJECT_ROOT / "data" / "morning_run.log"


# ---------------------------------------------------------------------------
# Plain-English output helpers
# ---------------------------------------------------------------------------

def banner(text: str) -> None:
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


def step(n: int, total: int, text: str) -> None:
    print()
    print(f"[{n}/{total}] {text}")


def info(text: str) -> None:
    print(f"       {text}")


def friendly_error(title: str, detail: str, suggestion: str) -> None:
    print()
    print("!" * 60)
    print(f"  PROBLEM: {title}")
    print(f"  Details: {detail}")
    print(f"  What to do: {suggestion}")
    print("!" * 60)


# ---------------------------------------------------------------------------
# Lock file (prevents double-clicks from running two mornings at once)
# ---------------------------------------------------------------------------

def acquire_lock() -> bool:
    """Return True if we grabbed the lock; False if another run is in progress."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            existing_pid = int(LOCK_FILE.read_text().strip() or "0")
        except ValueError:
            existing_pid = 0
        # Stale lock check: if the old PID is gone, it's safe to overwrite.
        if existing_pid and _pid_alive(existing_pid):
            return False
    LOCK_FILE.write_text(str(os.getpid()), encoding="ascii")
    return True


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Setup logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Only add our file handler; the console stays clean for the employee.
    root.addHandler(fh)


# ---------------------------------------------------------------------------
# Step 1 — Miami-Dade scraper
# ---------------------------------------------------------------------------

def run_miami_dade() -> dict:
    """
    Pull violations since the last successful run (up to today). The connector's
    own watermark file tracks where we left off, so if yesterday's run was
    skipped (weekend, sick day, etc.) today's run catches up automatically.
    """
    from connectors import miami_dade_unincorporated as md

    try:
        summary = md.run(headless=True)  # no dates -> uses watermark..today
        return {"inserted": summary.get("inserted", 0),
                "updated":  summary.get("updated", 0),
                "matched":  summary.get("matched", 0),
                "start":    summary.get("start"),
                "end":      summary.get("end"),
                "error":    None}
    except urllib.error.URLError as e:
        return {"inserted": 0, "updated": 0, "matched": 0, "start": None, "end": None,
                "error": f"Can't reach the Miami-Dade website ({e}). Check your internet."}
    except Exception as e:
        logging.exception("miami_dade run failed")
        return {"inserted": 0, "updated": 0, "matched": 0, "start": None, "end": None,
                "error": f"Miami-Dade pull failed: {e}. Tell Victor and try again later."}


# ---------------------------------------------------------------------------
# Step 2 — Homestead inbox
# ---------------------------------------------------------------------------

def run_homestead() -> dict:
    """Process any .xlsx files in inbox/homestead/. Returns {files, inserted, updated, error?}."""
    from connectors import homestead

    try:
        result = homestead.run()
        total_inserted = sum(r.get("inserted", 0) for r in result.get("results", []))
        total_updated  = sum(r.get("updated",  0) for r in result.get("results", []))
        return {"files": result.get("files_processed", 0),
                "inserted": total_inserted,
                "updated":  total_updated,
                "error":    None}
    except Exception as e:
        logging.exception("homestead run failed")
        return {"files": 0, "inserted": 0, "updated": 0,
                "error": f"Homestead file processing failed: {e}. Tell Victor."}


def run_homestead_tyler() -> dict:
    """
    Pull new Notice-of-Violation cases from Homestead's Tyler EnerGov portal.
    Idempotent. Uses an internal watermark so subsequent runs only fetch rows
    newer than the highest case number we've seen before.
    """
    from connectors.tyler_energov import run as tyler_run

    try:
        s = tyler_run("homestead")
        return {"fetched":  s.get("fetched", 0),
                "in_scope": s.get("in_scope", 0),
                "inserted": s.get("inserted", 0),
                "updated":  s.get("updated", 0),
                "error":    None}
    except Exception as e:
        logging.exception("homestead tyler run failed")
        return {"fetched": 0, "in_scope": 0, "inserted": 0, "updated": 0,
                "error": f"Homestead Tyler pull failed: {e}. Tell Victor."}


def retry_homestead_owner_enrichment() -> dict:
    """
    Re-run the Property Appraiser owner lookup for every Homestead row that
    still has no owner data. Useful when Tyler pull succeeded but the PA
    lookups failed (transient PA outage, rate limiting, etc.) so the rows
    sit in the DB unmailable.

    Idempotent. Counts the eligible rows up-front so the caller can show
    a real before/after delta.
    """
    import sqlite3
    from db import DB_PATH
    from connectors.tyler_energov import _enrich_owners_for

    def _unenriched_count() -> int:
        with sqlite3.connect(DB_PATH) as conn:
            r = conn.execute(
                """
                SELECT COUNT(*) FROM violations
                 WHERE source = 'homestead'
                   AND folio_number IS NOT NULL AND folio_number <> ''
                   AND ( owner_full_name      IS NULL OR owner_full_name      = ''
                      OR owner_mailing_address IS NULL OR owner_mailing_address = ''
                      OR comments LIKE '%NEEDS_OWNER_LOOKUP%' )
                """
            ).fetchone()
        return int(r[0])

    log = logging.getLogger("homestead")
    try:
        before = _unenriched_count()
        if before == 0:
            return {"before": 0, "enriched": 0, "after": 0, "error": None}
        enriched = _enrich_owners_for("homestead", log=log)
        after = _unenriched_count()
        return {"before": before, "enriched": enriched, "after": after, "error": None}
    except Exception as e:
        logging.exception("homestead owner enrichment retry failed")
        return {"before": 0, "enriched": 0, "after": 0,
                "error": f"Enrichment retry failed: {e}. Tell Victor."}


def run_homestead_tyler_since(since_case_number: str, near_page: int = 1) -> dict:
    """
    Recovery action: rewrite the Tyler watermark to a specific case number
    and pull everything strictly newer. Use when the incremental pull is
    stuck (page hint past the catalog tail, returning 0 fetched) or to
    backfill from a known boundary.

    Args:
      since_case_number: e.g. "CC-26-01806-NOV". Cases strictly greater than
                         this in lex order are collected.
      near_page:         Page where the given case roughly sits in the
                         Tyler catalog. Pass 1 to walk from the start
                         (~3 min); pass the actual rough page to skip the
                         ascending walk and finish in ~30 sec.
    """
    from connectors.tyler_energov import (
        run as tyler_run,
        _save_watermark,
        _save_watermark_page,
    )

    try:
        _save_watermark("homestead", since_case_number)
        _save_watermark_page("homestead", max(1, int(near_page)))
        logging.info("homestead tyler: watermark rewritten to %s (page hint %d)",
                     since_case_number, near_page)
        # Higher max_pages here because a stale page hint could put us far
        # below the actual catalog tail; we want to walk the rest in one go.
        s = tyler_run("homestead", max_pages=600)
        return {"fetched":  s.get("fetched", 0),
                "in_scope": s.get("in_scope", 0),
                "inserted": s.get("inserted", 0),
                "updated":  s.get("updated", 0),
                "error":    None}
    except Exception as e:
        logging.exception("homestead tyler backfill failed")
        return {"fetched": 0, "in_scope": 0, "inserted": 0, "updated": 0,
                "error": f"Homestead backfill failed: {e}. Tell Victor."}


def run_pinecrest_etrakit() -> dict:
    """
    Pull new code cases from Pinecrest's eTRAKiT portal. Idempotent — the
    connector keeps a date watermark so each run only fetches cases STARTED
    on or after the last successful run.
    """
    from connectors.etrakit import run as etrakit_run

    try:
        s = etrakit_run("pinecrest")
        return {"fetched":  s.get("fetched", 0),
                "in_scope": s.get("in_scope", 0),
                "inserted": s.get("inserted", 0),
                "updated":  s.get("updated", 0),
                "error":    None}
    except Exception as e:
        logging.exception("pinecrest eTRAKiT run failed")
        return {"fetched": 0, "in_scope": 0, "inserted": 0, "updated": 0,
                "error": f"Pinecrest pull failed: {e}. Tell Victor."}


# Palmetto Bay Eden auto-pull was disabled 2026-06-03 under the violations-only
# scope rule (the Eden portal only carries pending PSS-trade permits, not code
# violations). Palmetto Bay is now PRR-only via JustFOIA — see /upload-prr.


# ---------------------------------------------------------------------------
# Step 3 — Show totals + confirm + send
# ---------------------------------------------------------------------------

def fetch_totals() -> dict:
    """Count rows ready to mail. Safe even on an empty DB."""
    from db import connect

    with connect() as conn:
        try:
            total = conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
            ready = conn.execute("""
                SELECT COUNT(*) FROM violations
                WHERE owner_mailing_address IS NOT NULL
                  AND owner_full_name      IS NOT NULL
                  AND lob_letter_id        IS NULL
                  AND (comments NOT LIKE '%NEEDS_OWNER_LOOKUP%' OR comments IS NULL)
            """).fetchone()[0]
            sent = conn.execute(
                "SELECT COUNT(*) FROM violations WHERE lob_letter_id IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            # Brand new DB — tables don't exist yet
            total = ready = sent = 0
    return {"total": total, "ready": ready, "sent": sent}


def ask_to_mail(ready: int) -> bool:
    """Return True only if the employee types YES in full."""
    print()
    print("-" * 60)
    if ready == 0:
        print("  No letters waiting. Nothing to mail today.")
        print("-" * 60)
        return False
    print(f"  Ready to mail: {ready} letters.")
    print()
    print("  To mail them, type  YES  then press Enter.")
    print("  Anything else (including 'y') means DO NOT mail.")
    print("-" * 60)
    try:
        answer = input("  > ").strip()
    except EOFError:
        return False
    return answer == "YES"


def run_send() -> dict:
    """Call the Lob sender. Returns {sent, skipped, failed, error?}."""
    from lob_sender.send import send_batch

    try:
        summary = send_batch(limit=None, dry_run=False)
        return {"sent":    summary.get("sent", 0),
                "skipped": summary.get("skipped", 0),
                "failed":  summary.get("failed", 0),
                "error":   None}
    except RuntimeError as e:
        # send_batch raises RuntimeError when env vars are missing
        return {"sent": 0, "skipped": 0, "failed": 0,
                "error": (f"Mail sending isn't set up yet: {e} "
                          f"Tell Victor — he needs to fill in the .env file.")}
    except Exception as e:
        logging.exception("lob send failed")
        return {"sent": 0, "skipped": 0, "failed": 0,
                "error": f"Mail sending hit a snag: {e}. Tell Victor."}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> int:
    setup_logging()
    logging.info("=== morning_run start ===")

    if not acquire_lock():
        banner("Already running")
        info("Another morning run is still going. Wait for that one to finish,")
        info("or if you know it crashed, delete data/.morning_run.lock and try again.")
        return 0

    try:
        today_str = dt.date.today().strftime("%A, %B %d, %Y")
        banner(f"Good morning! {today_str}")

        # --- Step 1: Miami-Dade
        step(1, 5, "Pulling new Miami-Dade cases...")
        md = run_miami_dade()
        if md["error"]:
            friendly_error("Miami-Dade pull didn't work",
                           md["error"],
                           "Check your internet and try again. If it keeps failing, tell Victor.")
        else:
            if md["start"] and md["end"]:
                info(f"Checked dates {md['start']} -> {md['end']}")
            info(f"{md['inserted']} new cases pulled ({md['updated']} already in the system)")

        # --- Step 2: Homestead Tyler (live API pull, replaces / supplements PRR)
        step(2, 5, "Pulling new Homestead violations from Tyler portal...")
        ht = run_homestead_tyler()
        if ht["error"]:
            friendly_error("Homestead Tyler pull didn't work",
                           ht["error"],
                           "The Miami-Dade step still worked. Tell Victor about Homestead.")
        else:
            info(f"{ht['fetched']} cases checked, "
                 f"{ht['inserted']} new permit/zoning leads added")

        # --- Step 3: Pinecrest eTRAKiT (live portal pull)
        step(3, 5, "Pulling new Pinecrest cases from eTRAKiT portal...")
        pc = run_pinecrest_etrakit()
        if pc["error"]:
            friendly_error("Pinecrest pull didn't work",
                           pc["error"],
                           "The other steps still worked. Tell Victor about Pinecrest.")
        else:
            info(f"{pc['fetched']} cases checked, "
                 f"{pc['inserted']} new in-scope leads added")

        # --- Step 4: Homestead PRR Excel inbox (legacy fallback)
        step(4, 5, "Processing Homestead PRR spreadsheets...")
        hs = run_homestead()
        if hs["error"]:
            friendly_error("Homestead files didn't process",
                           hs["error"],
                           "The scrape step still worked. Tell Victor about the Homestead side.")
        elif hs["files"] == 0:
            info("No files in the Homestead PRR inbox. Skipped.")
        else:
            info(f"{hs['files']} file(s) processed — {hs['inserted']} new records added")

        # --- Step 5: Totals + confirm + send
        step(5, 5, "Today's numbers")
        totals = fetch_totals()
        info(f"Total cases in system  : {totals['total']}")
        info(f"Ready to mail          : {totals['ready']}")
        info(f"Already mailed so far  : {totals['sent']}")

        if not ask_to_mail(totals["ready"]):
            banner("Done. No letters mailed this run.")
            info("Clock out whenever you're ready.")
            return 0

        print()
        info(f"Mailing {totals['ready']} letters... (this can take a few minutes)")
        send = run_send()
        if send["error"]:
            friendly_error("Mailing didn't finish",
                           send["error"],
                           "The system remembers where it stopped. Running this again is safe.")
            return 1

        banner("All done")
        info(f"Mailed     : {send['sent']}")
        info(f"Skipped    : {send['skipped']}  (missing info — nothing wrong)")
        info(f"Failed     : {send['failed']}  (safe to re-run — won't double-mail)")
        print()
        info("You're good. Safe to clock out or answer the phone.")
        return 0

    except KeyboardInterrupt:
        banner("Stopped by you (Ctrl+C)")
        info("Nothing was half-mailed. It's safe to run this again.")
        return 0
    except Exception as e:
        logging.exception("morning_run crashed")
        friendly_error("Something unexpected broke",
                       str(e),
                       "Send Victor the data/morning_run.log file. Running again is safe.")
        return 1
    finally:
        release_lock()
        logging.info("=== morning_run end ===")


if __name__ == "__main__":
    sys.exit(main())
