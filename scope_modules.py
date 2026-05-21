"""
Reusable scope-of-services modules.

A "scope of services" section on an invoice is assembled from one or more
named modules (compliance_review, permit_preparation, ...) with per-job
variables substituted in (jurisdiction, fence_type, etc.). Edit the modules
once in Settings -> Scope Modules; reuse them on every invoice.

Conventions:
- key:   machine slug, lowercase, snake_case, immutable (e.g. 'compliance_review')
- name:  human label shown in the UI (e.g. 'Compliance Review')
- body:  the prose, may contain {{variables}}
- category: free-form filter ('fence', 'permit', 'general', ...) so the
            invoice form can show only relevant modules

Variable syntax: double curlies, e.g. {{jurisdiction}}, {{fence_type}}.
Unknown variables are left in place so it's obvious what wasn't supplied.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from db import connect


_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_KEY_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    """Lowercase name -> snake_case key. Strips punctuation and trims."""
    s = (name or "").strip().lower()
    s = _KEY_SAFE_RE.sub("_", s).strip("_")
    return s or "module"


def _unique_key(base: str) -> str:
    """If `base` already exists, append _2, _3, ... until we find a free one."""
    candidate = base
    n = 2
    while get_by_key(candidate):
        candidate = f"{base}_{n}"
        n += 1
    return candidate


# ---------- CRUD ----------

def list_modules(category: str | None = None) -> list[dict]:
    sql = "SELECT * FROM scope_modules"
    params: list = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY sort_order ASC, name COLLATE NOCASE ASC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_module(module_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scope_modules WHERE id = ?",
                           (module_id,)).fetchone()
    return dict(row) if row else None


def get_by_key(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scope_modules WHERE key = ?",
                           (key,)).fetchone()
    return dict(row) if row else None


def list_categories() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM scope_modules "
            "WHERE category IS NOT NULL AND category <> '' "
            "ORDER BY category COLLATE NOCASE"
        ).fetchall()
    return [r[0] for r in rows]


def create_module(*, name: str, key: str | None = None, body: str | None = None,
                  category: str | None = None, sort_order: int = 100) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Module name is required.")
    # Caller can pass an explicit key (seed data, scripts) but the UI no longer
    # exposes it — we auto-slug from the name and uniquify if needed.
    key = (key or "").strip().lower().replace(" ", "_") or _unique_key(_slugify(name))
    if get_by_key(key):
        raise ValueError(f"A module with key {key!r} already exists.")
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO scope_modules (key, name, body, category, sort_order, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, name, body, (category or None), int(sort_order), now, now),
        )
        new_id = cur.lastrowid
    return get_module(new_id)  # type: ignore[return-value]


def update_module(module_id: int, **fields) -> dict:
    existing = get_module(module_id)
    if not existing:
        raise LookupError(f"Module {module_id} not found.")

    settable = {"name", "body", "category", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in settable}
    if "name" in updates:
        nm = (updates["name"] or "").strip()
        if not nm:
            raise ValueError("Module name cannot be blank.")
        updates["name"] = nm
    if "sort_order" in updates:
        try:
            updates["sort_order"] = int(updates["sort_order"])
        except (TypeError, ValueError):
            updates["sort_order"] = 100
    if not updates:
        return existing

    updates["updated_at"] = _now()
    with connect() as conn:
        sets = ", ".join(f"{k} = :{k}" for k in updates)
        updates["id"] = module_id
        conn.execute(f"UPDATE scope_modules SET {sets} WHERE id = :id", updates)
    return get_module(module_id)  # type: ignore[return-value]


def delete_module(module_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM scope_modules WHERE id = ?", (module_id,))


# ---------- Assembly ----------

def render(text: str | None, variables: dict | None = None) -> str:
    """Substitute {{key}} variables in `text`. Unknown keys are left intact."""
    if not text:
        return ""
    vars_ = variables or {}
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in vars_ and vars_[key] is not None and str(vars_[key]).strip():
            return str(vars_[key])
        return m.group(0)  # leave {{unknown}} visible
    return _VAR_RE.sub(repl, text)


def assemble(module_keys: list[str], variables: dict | None = None,
             separator: str = "\n\n") -> str:
    """
    Look up each module by key (in the order given), render its body with
    the variables, and join with the separator. Missing modules are silently
    skipped (with their key embedded as a comment for debug).
    """
    out: list[str] = []
    for k in module_keys:
        k = (k or "").strip()
        if not k:
            continue
        mod = get_by_key(k)
        if not mod:
            out.append(f"<!-- scope_module not found: {k} -->")
            continue
        body = (mod.get("body") or "").strip()
        if body:
            out.append(render(body, variables))
    return separator.join(out)


# ---------- Seed defaults ----------

DEFAULT_MODULES = [
    {
        "key": "compliance_review",
        "name": "Compliance Review",
        "category": "general",
        "sort_order": 10,
        "body": (
            "Review existing installation for general compliance with applicable "
            "{{jurisdiction}} and/or municipal code requirements. Verify "
            "{{subject}} height, placement, gate requirements, and general "
            "installation conditions. Identify visible deficiencies requiring "
            "correction prior to permit approval or inspection."
        ),
    },
    {
        "key": "permit_preparation",
        "name": "Permit Preparation",
        "category": "general",
        "sort_order": 20,
        "body": (
            "Preparation of permit application package, including supporting "
            "documentation, material details, and required forms associated "
            "with the proposed or existing installation."
        ),
    },
    {
        "key": "engineering_coordination",
        "name": "Engineering Coordination",
        "category": "general",
        "sort_order": 30,
        "body": (
            "Coordination of after-the-fact installation certification "
            "documentation signed and sealed by a licensed professional "
            "engineer, when required by the jurisdiction."
        ),
    },
    {
        "key": "permit_processing",
        "name": "Permit Processing",
        "category": "general",
        "sort_order": 40,
        "body": (
            "Submission and processing of permit application with the "
            "applicable jurisdiction. Monitoring permit review status and "
            "coordination of standard responses to review comments related to "
            "the submitted scope of work."
        ),
    },
    {
        "key": "inspection_coordination",
        "name": "Inspection Coordination",
        "category": "general",
        "sort_order": 50,
        "body": (
            "Coordination of required inspections and follow-through until "
            "permit receives final approved status, provided installation "
            "complies with applicable code requirements."
        ),
    },
    {
        "key": "exclusions",
        "name": "Exclusions",
        "category": "general",
        "sort_order": 90,
        "body": (
            "Fees associated with municipal penalties, re-inspections, "
            "additional corrections, undisclosed violations, structural "
            "modifications, surveying services, and additional engineering "
            "services beyond the original scope are excluded unless "
            "specifically stated otherwise."
        ),
    },
]


def seed_defaults(force: bool = False) -> int:
    """
    Insert the standard modules from spec. Skips any whose key already exists
    unless force=True (which updates them in place). Returns count inserted/updated.
    """
    count = 0
    for spec in DEFAULT_MODULES:
        existing = get_by_key(spec["key"])
        if existing and not force:
            continue
        if existing and force:
            update_module(existing["id"], name=spec["name"], body=spec["body"],
                          category=spec["category"], sort_order=spec["sort_order"])
        else:
            create_module(**spec)
        count += 1
    return count
