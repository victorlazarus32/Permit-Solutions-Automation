"""
Permit Solutions Operator Console.

Internal Flask app that lets a non-technical employee run the daily lead
pipeline end-to-end through a browser instead of the command line.

Pages:
  /            -- Dashboard: today's counts, recent activity, action buttons.
  /queue       -- Letters queued and ready to mail.
  /sent        -- Letters already mailed, with current Lob status.
  /settings    -- Lob credentials status and test-send button.
  /actions/... -- POST endpoints for each daily action.

Run locally:
    python -m app.server
Then open http://localhost:8000 in a browser.

Authentication:
    v1 binds to 127.0.0.1 (loopback only). The employee uses this on the
    same machine where the data lives. If you ever want to expose this on
    the LAN, add HTTP Basic via APP_PASSWORD in .env and bind to 0.0.0.0.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

import json
import secrets as _secrets
import socket

from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_file, session
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from db import DB_PATH
from scripts.morning_run import (run_miami_dade, run_homestead,
                                  run_homestead_tyler, fetch_totals, run_send)

LOG_FILE = PROJECT_ROOT / "data" / "morning_run.log"
APP_LOG_FILE = PROJECT_ROOT / "data" / "operator_console.log"
WATERMARK_FILE = PROJECT_ROOT / "data" / "miami_dade_unincorporated_last_run.txt"
USERS_FILE = PROJECT_ROOT / "data" / "users.json"
SECRET_FILE = PROJECT_ROOT / "data" / ".app_secret"

def _load_or_create_secret() -> str:
    """Persist a stable session secret so logins survive server restarts."""
    if os.environ.get("APP_SECRET"):
        return os.environ["APP_SECRET"]
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="ascii").strip()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    secret = _secrets.token_hex(32)
    SECRET_FILE.write_text(secret, encoding="ascii")
    return secret


def _load_users() -> dict:
    """Username -> hashed password. Empty dict if file missing or invalid."""
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
    static_folder=str(Path(__file__).resolve().parent / "static"),
)
app.secret_key = _load_or_create_secret()


# Endpoints that bypass the login gate.
_OPEN_ENDPOINTS = {"login", "static"}


@app.before_request
def _require_login():
    """Gate every endpoint except /login and static files."""
    if request.endpoint is None:
        return None
    if request.endpoint in _OPEN_ENDPOINTS:
        return None
    if request.path.startswith("/static/"):
        return None
    if session.get("user"):
        return None

    # Not authenticated. For HTML routes redirect to login; for JSON return 401.
    if request.path.startswith("/api/"):
        return jsonify({"error": "not authenticated"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        users = _load_users()
        if username in users and check_password_hash(users[username], password):
            session["user"] = username
            session.permanent = True
            target = request.args.get("next") or url_for("dashboard")
            log.info("user %s signed in from %s", username, request.remote_addr)
            return redirect(target)
        flash("Invalid username or password.", "error")
        log.warning("login failed for %r from %s", username, request.remote_addr)

    no_users = (len(_load_users()) == 0)
    return render_template("login.html", no_users=no_users)


@app.route("/logout")
def logout():
    user = session.pop("user", None)
    if user:
        log.info("user %s signed out", user)
    return redirect(url_for("login"))


@app.context_processor
def _inject_globals() -> dict:
    """Make the current timestamp + logged-in user available to every template."""
    return {
        "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "current_user": session.get("user"),
    }

# Tracks the most recent long-running task so the UI can show progress.
# Single global because the operator console is single-user, single-tenant.
TASK_STATE: dict = {
    "running": False,
    "kind": None,           # "scrape" | "send" | None
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "progress": None,       # {stage, done, total, case_number, address}
}
TASK_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(APP_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("operator_console")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_last_scrape() -> str | None:
    if WATERMARK_FILE.exists():
        return WATERMARK_FILE.read_text(encoding="utf-8").strip()
    return None


def _read_last_log_lines(n: int = 25) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-n:]
        return [line.rstrip() for line in tail]
    except Exception as e:
        return [f"(could not read log: {e})"]


def _last_send_time() -> str | None:
    """Most recent lob_mailed_at timestamp from the DB."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT MAX(lob_mailed_at) FROM violations WHERE lob_letter_id IS NOT NULL"
            ).fetchone()
            return row[0] if row and row[0] else None
    except sqlite3.OperationalError:
        return None


def _env_status() -> dict:
    """Mask sensitive credentials but report which slots are filled."""
    def mask(name: str, val: str) -> dict:
        if not val or "PLACEHOLDER" in val:
            return {"name": name, "set": False, "preview": ""}
        if len(val) > 12:
            return {"name": name, "set": True, "preview": f"{val[:8]}..."}
        return {"name": name, "set": True, "preview": val}

    api = os.environ.get("LOB_API_KEY", "")
    mode = ""
    if api and "PLACEHOLDER" not in api:
        mode = "LIVE" if api.startswith("live_") else "TEST" if api.startswith("test_") else "?"

    return {
        "lob_api_key":         mask("LOB_API_KEY", api),
        "lob_mode":            mode,
        "lob_from_address_id": mask("LOB_FROM_ADDRESS_ID", os.environ.get("LOB_FROM_ADDRESS_ID", "")),
        "lob_template_id":     mask("LOB_TEMPLATE_ID",     os.environ.get("LOB_TEMPLATE_ID", "")),
        "test_recipient_name": mask("TEST_RECIPIENT_NAME", os.environ.get("TEST_RECIPIENT_NAME", "")),
        "test_recipient_line1":mask("TEST_RECIPIENT_LINE1",os.environ.get("TEST_RECIPIENT_LINE1","")),
        "test_recipient_city": mask("TEST_RECIPIENT_CITY", os.environ.get("TEST_RECIPIENT_CITY", "")),
        "test_recipient_state":mask("TEST_RECIPIENT_STATE",os.environ.get("TEST_RECIPIENT_STATE","")),
        "test_recipient_zip":  mask("TEST_RECIPIENT_ZIP",  os.environ.get("TEST_RECIPIENT_ZIP", "")),
    }


def _by_source_counts() -> dict:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM violations GROUP BY source"
            ).fetchall()
            return dict(rows)
    except sqlite3.OperationalError:
        return {}


def _ready_rows(limit: int = 500) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT source, case_number, open_date, property_address,
                       owner_full_name, owner_mailing_address,
                       matched_keywords, alleged_violation
                FROM violations
                WHERE owner_mailing_address IS NOT NULL
                  AND owner_full_name      IS NOT NULL
                  AND lob_letter_id        IS NULL
                  AND (comments NOT LIKE '%NEEDS_OWNER_LOOKUP%' OR comments IS NULL)
                ORDER BY open_date DESC, case_number DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _calculate_lead_score(d: dict) -> tuple[int, str]:
    """
    Score an intake from 0-100 using a weighted heuristic, then map to a
    temperature (hot / warm / cold). Senior officials still make the final call;
    this just surfaces the strongest signals at a glance.
    """
    score = 40  # baseline; HOT requires real signal stacking, not just being a homeowner

    # Authority: who is on the call?
    if d.get("is_property_owner"):
        score += 18
    if d.get("has_permission"):
        score += 4

    # Urgency
    score += {"critical": 14, "high": 10, "medium": 5}.get(d.get("urgency"), 0)

    # Motivation: hard deadlines beat soft ones
    motiv = d.get("primary_motivation")
    score += {
        "sale": 14,
        "refi": 12,
        "avoid_lien": 12,
        "fines": 8,
        "compliance": 4,
        "family": 3,
    }.get(motiv, 0)

    # Money & competition
    if d.get("budget_aware"):
        score += 5
    if d.get("has_other_quotes") == 0:   # explicitly "no other quotes"
        score += 8
    if d.get("has_other_quotes") == 1:   # they're shopping
        score -= 3

    # Tangible problem markers
    if d.get("lien_filed"):
        score += 6
    if (d.get("fines_accrued_usd") or 0) > 0:
        score += 4
    if d.get("court_date"):
        score += 6

    # Scope match
    types = (d.get("violation_types") or "").lower()
    if any(t in types for t in ("fence","gate","door","window","garage","pergola","terrace","shed")):
        score += 8

    # Friction signals
    if d.get("has_hired_before"):
        score -= 4   # been burned before, harder to close fast
    if not d.get("is_property_owner"):
        score -= 12  # not the owner is a real problem

    # Disposition signal (operator already declared intent)
    score += {"book_consult": 8, "send_quote": 6, "contract_signed": 20}.get(d.get("disposition"), 0)

    score = max(0, min(100, int(score)))
    if score >= 85:
        temp = "hot"
    elif score >= 65:
        temp = "warm"
    else:
        temp = "cold"
    return score, temp


def _list_intakes(limit: int = 200) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT li.*, v.owner_full_name, v.property_address, v.matched_keywords
                FROM lead_intakes li
                LEFT JOIN violations v
                  ON v.source = li.source AND v.case_number = li.case_number
                ORDER BY
                  CASE li.senior_review_status
                    WHEN 'pending'  THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'rejected' THEN 2
                  END,
                  li.lead_score DESC,
                  li.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _intake_review_counts() -> dict:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT senior_review_status, COUNT(*) FROM lead_intakes GROUP BY senior_review_status"
            ).fetchall()
            return {k: v for k, v in rows}
    except sqlite3.OperationalError:
        return {}


def _conversion_metrics() -> dict:
    """Conversion funnel: mailed -> calls -> contracts, plus revenue."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            mailed = conn.execute(
                "SELECT COUNT(*) FROM violations WHERE lob_letter_id IS NOT NULL"
            ).fetchone()[0]
            calls = conn.execute(
                """
                SELECT COUNT(DISTINCT source || '|' || case_number)
                FROM pipeline_events
                WHERE event_type IN ('call','text','email','meeting')
                """
            ).fetchone()[0]
            contracts = conn.execute(
                """
                SELECT COUNT(DISTINCT source || '|' || case_number)
                FROM pipeline_events
                WHERE event_type = 'contract'
                """
            ).fetchone()[0]
            revenue = conn.execute(
                "SELECT COALESCE(SUM(contract_value), 0) FROM pipeline_events WHERE event_type='contract'"
            ).fetchone()[0]
    except sqlite3.OperationalError:
        mailed, calls, contracts, revenue = 0, 0, 0, 0.0

    def pct(n, d):
        return round((n / d) * 100, 1) if d else 0.0

    return {
        "mailed":          mailed,
        "calls":           calls,
        "contracts":       contracts,
        "revenue":         float(revenue or 0),
        "pct_call":        pct(calls, mailed),
        "pct_contract":    pct(contracts, mailed),
        "pct_close":       pct(contracts, calls),
    }


def _list_events(limit: int = 200) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT pe.id, pe.source, pe.case_number, pe.event_type, pe.occurred_at,
                       pe.contact_name, pe.contact_phone, pe.contact_email,
                       pe.contract_value, pe.notes, pe.created_at,
                       v.owner_full_name, v.property_address
                FROM pipeline_events pe
                LEFT JOIN violations v
                  ON v.source = pe.source AND v.case_number = pe.case_number
                ORDER BY pe.occurred_at DESC, pe.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _case_options(limit: int = 1000) -> list[dict]:
    """Recently mailed cases (preferred for dropdown), then ready-to-mail as fallback."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT source, case_number, owner_full_name, property_address,
                       lob_letter_id IS NOT NULL AS is_mailed
                FROM violations
                ORDER BY (lob_letter_id IS NOT NULL) DESC,
                         lob_mailed_at DESC,
                         open_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _violation_row(source: str, case_number: str) -> dict | None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM violations WHERE source=? AND case_number=?",
                (source, case_number),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


def _events_for_case(source: str, case_number: str) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM pipeline_events
                WHERE source=? AND case_number=?
                ORDER BY occurred_at DESC, id DESC
                """,
                (source, case_number),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _intakes_for_case(source: str, case_number: str) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM lead_intakes WHERE source=? AND case_number=? ORDER BY created_at DESC",
                (source, case_number),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _overdue_actions(limit: int = 25) -> list[dict]:
    """Pending intakes with a next_action_at in the past or within 3 days."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT li.id, li.source, li.case_number, li.next_action,
                       li.next_action_at, li.lead_temperature, li.lead_score,
                       li.disposition, li.senior_review_status,
                       v.owner_full_name, v.property_address
                FROM lead_intakes li
                LEFT JOIN violations v
                  ON v.source = li.source AND v.case_number = li.case_number
                WHERE li.next_action_at IS NOT NULL
                  AND li.next_action_at <= date('now', '+3 days')
                  AND li.disposition NOT IN ('not_qualified','contract_signed')
                ORDER BY li.next_action_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _daily_lead_counts(days: int = 30) -> list[dict]:
    """Number of cases first_seen per day, last N days."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT date(first_seen_at) AS d, COUNT(*) AS n
                FROM violations
                WHERE first_seen_at >= date('now', ?)
                GROUP BY date(first_seen_at)
                ORDER BY d ASC
                """,
                (f"-{int(days)} days",),
            ).fetchall()
        return [{"d": r[0], "n": r[1]} for r in rows]
    except sqlite3.OperationalError:
        return []


def _revenue_by_day(days: int = 90) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT date(occurred_at) AS d, SUM(contract_value) AS rev
                FROM pipeline_events
                WHERE event_type='contract'
                  AND occurred_at >= date('now', ?)
                GROUP BY date(occurred_at)
                ORDER BY d ASC
                """,
                (f"-{int(days)} days",),
            ).fetchall()
        return [{"d": r[0], "rev": float(r[1] or 0)} for r in rows]
    except sqlite3.OperationalError:
        return []


def _quick_search(q: str, limit: int = 12) -> list[dict]:
    """Match across case_number, owner, property_address. Case-insensitive substring."""
    if not q or len(q.strip()) < 2:
        return []
    needle = f"%{q.strip()}%"
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT source, case_number, owner_full_name, property_address,
                       lob_letter_id IS NOT NULL AS is_mailed
                FROM violations
                WHERE case_number      LIKE ?
                   OR owner_full_name  LIKE ?
                   OR property_address LIKE ?
                ORDER BY (lob_letter_id IS NOT NULL) DESC, open_date DESC
                LIMIT ?
                """,
                (needle, needle, needle, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _sent_rows(limit: int = 500) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT source, case_number, owner_full_name, property_address,
                       lob_letter_id, lob_status, lob_mailed_at, lob_last_event_at
                FROM violations
                WHERE lob_letter_id IS NOT NULL
                ORDER BY lob_mailed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _start_task(kind: str, target, *args) -> bool:
    """Kick off a background task. Returns False if another task is already running."""
    with TASK_LOCK:
        if TASK_STATE["running"]:
            return False
        TASK_STATE.update({
            "running": True,
            "kind": kind,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "result": None,
            "error": None,
            "progress": None,
        })

    def _wrapper():
        try:
            result = target(*args)
            with TASK_LOCK:
                TASK_STATE["result"] = result
        except Exception as e:
            log.exception("task %s failed", kind)
            with TASK_LOCK:
                TASK_STATE["error"] = str(e)
        finally:
            with TASK_LOCK:
                TASK_STATE["running"] = False
                TASK_STATE["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_wrapper, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    totals = fetch_totals()
    by_source = _by_source_counts()
    last_scrape = _read_last_scrape()
    last_send = _last_send_time()
    funnel = _conversion_metrics()
    overdue = _overdue_actions(limit=10)
    daily   = _daily_lead_counts(days=30)
    revenue = _revenue_by_day(days=90)

    with TASK_LOCK:
        task = dict(TASK_STATE)

    lob_ok, lob_reason = _lob_ready()

    return render_template(
        "dashboard.html",
        totals=totals,
        by_source=by_source,
        last_scrape=last_scrape,
        last_send=last_send,
        funnel=funnel,
        task=task,
        overdue=overdue,
        daily=daily,
        revenue=revenue,
        lob_ok=lob_ok,
        lob_reason=lob_reason,
        today=dt.date.today().strftime("%A, %B %d, %Y"),
    )


@app.route("/lead/<source>/<case_number>")
def lead_detail(source: str, case_number: str):
    violation = _violation_row(source, case_number)
    if not violation:
        flash(f"No case found for {source}/{case_number}.", "error")
        return redirect(url_for("dashboard"))
    events  = _events_for_case(source, case_number)
    intakes = _intakes_for_case(source, case_number)
    return render_template(
        "lead_detail.html",
        v=violation,
        events=events,
        intakes=intakes,
        today_iso=dt.date.today().isoformat(),
    )


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    return jsonify({"results": _quick_search(q, limit=12)})


@app.route("/pipeline")
def pipeline():
    intakes = _list_intakes(limit=200)
    review_counts = _intake_review_counts()
    cases = _case_options(limit=500)
    funnel = _conversion_metrics()
    return render_template(
        "pipeline.html",
        intakes=intakes,
        review_counts=review_counts,
        cases=cases,
        funnel=funnel,
        today_iso=dt.date.today().isoformat(),
    )


def _ftb(form, name: str) -> int | None:
    """Form -> tri-state bool int. Returns 1, 0, or None for radios with values yes/no/unknown."""
    v = (form.get(name) or "").strip().lower()
    if v == "yes" or v == "1" or v == "true":  return 1
    if v == "no"  or v == "0" or v == "false": return 0
    return None


def _ff(form, name: str) -> float | None:
    """Form -> float, tolerant of $ and commas."""
    raw = (form.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _fi(form, name: str) -> int | None:
    raw = (form.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _fs(form, name: str) -> str | None:
    raw = (form.get(name) or "").strip()
    return raw or None


@app.post("/actions/log-intake")
def action_log_intake():
    """Save a comprehensive lead-intake record. Auto-calculates score + temperature."""
    f = request.form

    # Lead source: where did this lead come from? Drives whether we tie to a
    # known case or capture free-text property fields for an unsolicited lead.
    lead_source = (f.get("lead_source") or "mailed_letter").strip()
    if lead_source not in {"mailed_letter","unsolicited","referral","website","other"}:
        lead_source = "mailed_letter"

    if lead_source == "mailed_letter":
        # Required: case key tied to an existing violation row.
        src = (f.get("case_key") or "").split("|", 1)
        if len(src) != 2:
            flash("Pick a case from the dropdown before saving a mailed-letter intake.", "error")
            return redirect(url_for("pipeline"))
        source, case_number = src
        caller_property_address = None
        caller_jurisdiction     = None
    else:
        # Unsolicited / referral / website / other: no case in our DB yet.
        # Use a sentinel source + unique case_number so the row still satisfies
        # NOT NULL constraints and stays unique. Capture the property address
        # and jurisdiction the caller gave us in free-text fields.
        source = "unsolicited"
        case_number = f"UNSOLIC-{uuid.uuid4().hex[:10].upper()}"
        caller_property_address = (f.get("caller_property_address") or "").strip() or None
        caller_jurisdiction     = (f.get("caller_jurisdiction")     or "").strip() or None

    call_at = (f.get("call_at") or "").strip() or dt.date.today().isoformat()

    # Multi-select: violation_types, materials are sent as repeated form fields
    violation_types = ",".join(f.getlist("violation_types"))
    materials       = ",".join(f.getlist("materials"))

    record = {
        "source":                 source,
        "case_number":            case_number,
        "lead_source":            lead_source,
        "caller_property_address": caller_property_address,
        "caller_jurisdiction":    caller_jurisdiction,
        "call_at":                call_at,
        "inbound_channel":        _fs(f, "inbound_channel"),
        "caller_name":            _fs(f, "caller_name"),
        "caller_phone":           _fs(f, "caller_phone"),
        "caller_email":           _fs(f, "caller_email"),
        "best_callback_time":     _fs(f, "best_callback_time"),

        "is_property_owner":      _ftb(f, "is_property_owner"),
        "relationship_to_owner":  _fs(f, "relationship_to_owner"),
        "has_permission":         _ftb(f, "has_permission"),

        "notices_received_count": _fi(f, "notices_received_count"),
        "fines_accrued_usd":      _ff(f, "fines_accrued_usd"),
        "lien_filed":             _ftb(f, "lien_filed"),
        "court_date":             _fs(f, "court_date"),
        "inspector_contact":      _fs(f, "inspector_contact"),

        "primary_motivation":     _fs(f, "primary_motivation"),
        "urgency":                _fs(f, "urgency"),
        "has_tried_diy":          _ftb(f, "has_tried_diy"),
        "has_contacted_city":     _ftb(f, "has_contacted_city"),
        "has_hired_before":       _ftb(f, "has_hired_before"),
        "previous_contractor":    _fs(f, "previous_contractor"),

        "violation_types":        violation_types or None,
        "materials":              materials or None,
        "rough_linear_feet":      _ff(f, "rough_linear_feet"),
        "originally_permitted":   _ftb(f, "originally_permitted"),
        "currently_standing":     _ftb(f, "currently_standing"),

        "decision_maker":         _fs(f, "decision_maker"),
        "budget_aware":           _ftb(f, "budget_aware"),
        "has_other_quotes":       _ftb(f, "has_other_quotes"),
        "other_quotes_from":      _fs(f, "other_quotes_from"),
        "insurance_involved":     _ftb(f, "insurance_involved"),

        "target_resolution_date": _fs(f, "target_resolution_date"),
        "timeline_flexibility":   _fs(f, "timeline_flexibility"),
        "deadline_reason":        _fs(f, "deadline_reason"),

        "disposition":            _fs(f, "disposition"),
        "contract_value_usd":     _ff(f, "contract_value_usd"),
        "next_action":            _fs(f, "next_action"),
        "next_action_at":         _fs(f, "next_action_at"),
        "assigned_to":            _fs(f, "assigned_to"),
        "operator_notes":         _fs(f, "operator_notes"),
    }

    score, temperature = _calculate_lead_score(record)
    record["lead_score"] = score
    record["lead_temperature"] = temperature

    now = dt.datetime.now().isoformat(timespec="seconds")
    record["created_at"] = now
    record["updated_at"] = now

    # Insert
    cols = ", ".join(record.keys())
    placeholders = ", ".join(f":{k}" for k in record.keys())
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                f"INSERT INTO lead_intakes ({cols}) VALUES ({placeholders})",
                record,
            )
            new_id = cur.lastrowid

            # Mirror as a simple pipeline_event so the funnel keeps working.
            ev_type = "contract" if record["disposition"] == "contract_signed" else (
                record["inbound_channel"] or "call"
            )
            if ev_type not in {"call","text","email","meeting","contract","declined","no_response","note"}:
                ev_type = "call"
            conn.execute(
                """
                INSERT INTO pipeline_events (
                    source, case_number, event_type, occurred_at,
                    contact_name, contact_phone, contact_email,
                    contract_value, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source, case_number, ev_type, call_at,
                 record["caller_name"], record["caller_phone"], record["caller_email"],
                 record["contract_value_usd"],
                 record["operator_notes"] or f"Intake #{new_id}",
                 now),
            )
    except Exception as e:
        flash(f"Could not save intake: {e}", "error")
        return redirect(url_for("pipeline"))

    flash(
        f"Intake #{new_id} saved. Score {score}/100 ({temperature.upper()}). "
        f"Status: PENDING senior review.", "success",
    )
    return redirect(url_for("pipeline"))


@app.post("/actions/review-intake/<int:intake_id>")
def action_review_intake(intake_id: int):
    """Senior official approves or rejects a lead intake."""
    decision = (request.form.get("decision") or "").strip().lower()
    if decision not in {"approved", "rejected"}:
        flash("Pick approved or rejected.", "error")
        return redirect(url_for("pipeline"))

    reviewer = (request.form.get("reviewer") or "Victor").strip()
    notes = (request.form.get("review_notes") or "").strip() or None
    now = dt.datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE lead_intakes
               SET senior_review_status=?,
                   senior_reviewer=?,
                   senior_review_notes=?,
                   senior_reviewed_at=?,
                   updated_at=?
             WHERE id=?
            """,
            (decision, reviewer, notes, now, now, intake_id),
        )

    flash(f"Intake #{intake_id} marked {decision.upper()}.", "success")
    return redirect(url_for("pipeline"))


@app.post("/actions/log-event")
def action_log_event():
    """Insert a new pipeline event (call, text, contract, etc)."""
    src        = (request.form.get("case_key") or "").split("|", 1)
    if len(src) != 2:
        flash("Pick a case from the dropdown.", "error")
        return redirect(url_for("pipeline"))
    source, case_number = src

    event_type = (request.form.get("event_type") or "").strip()
    if event_type not in {"call","text","email","meeting","contract","declined","no_response","note"}:
        flash("Pick a valid event type.", "error")
        return redirect(url_for("pipeline"))

    occurred_at = (request.form.get("occurred_at") or "").strip()
    if not occurred_at:
        occurred_at = dt.date.today().isoformat()

    contact_name  = (request.form.get("contact_name")  or "").strip() or None
    contact_phone = (request.form.get("contact_phone") or "").strip() or None
    contact_email = (request.form.get("contact_email") or "").strip() or None
    notes         = (request.form.get("notes")         or "").strip() or None

    contract_value = None
    raw_value = (request.form.get("contract_value") or "").strip()
    if raw_value:
        try:
            contract_value = float(raw_value.replace(",", "").replace("$", ""))
        except ValueError:
            flash("Contract value must be a number.", "error")
            return redirect(url_for("pipeline"))

    now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO pipeline_events (
                    source, case_number, event_type, occurred_at,
                    contact_name, contact_phone, contact_email,
                    contract_value, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source, case_number, event_type, occurred_at,
                 contact_name, contact_phone, contact_email,
                 contract_value, notes, now),
            )
    except Exception as e:
        flash(f"Could not save event: {e}", "error")
        return redirect(url_for("pipeline"))

    label = event_type.replace("_", " ").title()
    if event_type == "contract" and contract_value:
        flash(f"Logged {label} for case {case_number}: ${contract_value:,.2f}", "success")
    else:
        flash(f"Logged {label} for case {case_number}", "success")
    return redirect(url_for("pipeline"))


@app.route("/queue")
def queue():
    rows = _ready_rows(limit=1000)
    return render_template("queue.html", rows=rows, count=len(rows))


@app.route("/sent")
def sent():
    rows = _sent_rows(limit=500)
    return render_template("sent.html", rows=rows, count=len(rows))


@app.route("/settings")
def settings():
    return render_template("settings.html", env=_env_status())


# ---------------------------------------------------------------------------
# Action endpoints (POST only — these change state)
# ---------------------------------------------------------------------------

@app.post("/actions/scrape")
def action_scrape():
    if not _start_task("scrape", run_miami_dade):
        flash("Another task is already running. Wait for it to finish.", "warning")
    else:
        flash("Pulling new Miami-Dade cases. This usually takes 10 to 30 seconds.", "info")
    return redirect(url_for("dashboard"))


@app.post("/actions/process-homestead")
def action_homestead():
    if not _start_task("homestead", run_homestead):
        flash("Another task is already running. Wait for it to finish.", "warning")
    else:
        flash("Processing PRR inbox files.", "info")
    return redirect(url_for("dashboard"))


@app.post("/actions/pull-homestead-tyler")
def action_homestead_tyler():
    if not _start_task("homestead_tyler", run_homestead_tyler):
        flash("Another task is already running. Wait for it to finish.", "warning")
    else:
        flash("Pulling new Homestead permit/zoning violations from the Tyler portal. "
              "This takes about a minute.", "info")
    return redirect(url_for("dashboard"))


# Cities that accept manual PRR uploads. Add to this list as new connectors come online.
_PRR_CITIES = ["homestead", "palmetto_bay", "cutler_bay", "pinecrest", "miami_beach"]


@app.route("/upload-prr", methods=["GET", "POST"])
def upload_prr():
    if request.method == "POST":
        city = (request.form.get("city") or "").strip().lower()
        if city not in _PRR_CITIES:
            flash("Pick a valid city from the dropdown.", "error")
            return redirect(url_for("upload_prr"))

        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("upload_prr"))
        if not f.filename.lower().endswith((".xlsx", ".xls")):
            flash("Only .xlsx or .xls files are accepted.", "error")
            return redirect(url_for("upload_prr"))

        safe_name = secure_filename(f.filename)
        dest_dir  = PROJECT_ROOT / "inbox" / city
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe_name
        f.save(str(dest))
        log.info("PRR uploaded by %s: %s -> %s", session.get("user"), f.filename, dest)
        flash(
            f"Uploaded {safe_name} to the {city.replace('_',' ').title()} inbox. "
            f"Click Process PRR on the dashboard to ingest it.",
            "success",
        )
        return redirect(url_for("dashboard"))

    return render_template("upload.html", cities=_PRR_CITIES)


def _lob_ready() -> tuple[bool, str | None]:
    """Are Lob credentials real (not placeholder, not blank)? Return (ok, reason)."""
    api = os.environ.get("LOB_API_KEY", "").strip()
    from_id = os.environ.get("LOB_FROM_ADDRESS_ID", "").strip()
    if not api or "PLACEHOLDER" in api:
        return False, "LOB_API_KEY is missing or still a placeholder. Open Settings to see what is needed."
    if not (api.startswith("test_") or api.startswith("live_")):
        return False, "LOB_API_KEY does not look like a real Lob key (should start with test_ or live_)."
    if not from_id or "PLACEHOLDER" in from_id or not from_id.startswith("adr_"):
        return False, "LOB_FROM_ADDRESS_ID is missing or still a placeholder. Create a return address in the Lob dashboard, then paste the adr_xxx ID into .env."
    return True, None


@app.post("/actions/send")
def action_send():
    if request.form.get("confirm") != "YES":
        flash('You must type "YES" (in capital letters) to authorize the day\'s mailing.', "warning")
        return redirect(url_for("dashboard"))

    ok, why = _lob_ready()
    if not ok:
        flash(f"Cannot send: {why}", "error")
        return redirect(url_for("dashboard"))

    def _send_with_progress():
        from lob_sender.send import send_batch
        def on_progress(snap):
            with TASK_LOCK:
                TASK_STATE["progress"] = snap
        try:
            summary = send_batch(limit=None, dry_run=False, on_progress=on_progress)
            return {
                "sent":    summary.get("sent", 0),
                "skipped": summary.get("skipped", 0),
                "failed":  summary.get("failed", 0),
                "error":   None,
            }
        except RuntimeError as e:
            return {"sent": 0, "skipped": 0, "failed": 0,
                    "error": (f"Mail sending isn't set up yet: {e} "
                              f"Tell Victor — he needs to fill in the .env file.")}

    if not _start_task("send", _send_with_progress):
        flash("Another task is already running. Wait for it to finish.", "warning")
    else:
        flash("Sending today's letters. The beacon up top tracks progress live.", "info")
    return redirect(url_for("dashboard"))


@app.post("/actions/test-send")
def action_test_send():
    """Run scripts.send_test_to_self in a subprocess so we don't tangle Flask threads with stdin prompts."""
    cmd = [sys.executable, "-m", "scripts.send_test_to_self", "--limit", "1"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
            input="y\n",   # in case the script asks "continue with non-live key?"
        )
        out = (proc.stdout or "") + ((proc.stderr or "") and ("\n[stderr]\n" + proc.stderr))
        if proc.returncode == 0:
            flash("Test letter queued at Lob. Check the Lob dashboard for the new letter.", "success")
        else:
            flash(f"Test send failed (exit {proc.returncode}). See log below.", "error")
        log.info("test send output:\n%s", out)
    except subprocess.TimeoutExpired:
        flash("Test send timed out after 2 minutes.", "error")
    return redirect(url_for("settings"))


@app.get("/api/task-status")
def api_task_status():
    with TASK_LOCK:
        return jsonify(dict(TASK_STATE))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _local_lan_ip() -> str:
    """Best-effort local IP that other LAN devices can reach this server at."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # no traffic actually sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    port = int(os.environ.get("APP_PORT", "8000"))
    host = os.environ.get("APP_HOST", "0.0.0.0")
    lan  = _local_lan_ip()
    log.info("==========================================================")
    log.info("Permit Solutions Operator Console")
    log.info("  Local URL  : http://localhost:%d", port)
    if host == "0.0.0.0":
        log.info("  LAN URL    : http://%s:%d", lan, port)
        log.info("  Share the LAN URL with anyone on the same network.")
    log.info("  Login required (manage users with scripts/manage_users.py)")
    log.info("==========================================================")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
