"""
Shared parsing utilities used by every connector.

Responsibilities:
  - Cell cleaning (whitespace, NaN, ASP.NET sentinels)
  - Date normalization to ISO YYYY-MM-DD
  - Keyword matching against the violation description field
  - Building the canonical row dict for upsert

Each connector is responsible for:
  - Loading its source file/URL into a DataFrame
  - Mapping its source columns to the canonical field names below
  - Calling build_record() per row

The canonical field names match the DB schema in db.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config.keywords import KEYWORD_PATTERNS, EXCLUSION_COOCCURRENCE

_KEYWORD_RE = re.compile("|".join(KEYWORD_PATTERNS), re.IGNORECASE)

# Compile once: each pair becomes (regex_a, regex_b). A row is excluded when
# BOTH patterns appear within the same sentence of the searched field.
_EXCLUSION_PAIRS = [
    (re.compile(a, re.IGNORECASE), re.compile(b, re.IGNORECASE))
    for a, b in EXCLUSION_COOCCURRENCE
]

# Split on terminal punctuation. Most violation descriptions are a single
# fragment with no period, but city portals occasionally produce multi-sentence
# narratives (e.g. "FENCE NO PERMIT. MILDEW ON ROOF.") and we want each
# sentence judged on its own merits — exclusion shouldn't poison a row whose
# OTHER sentence is genuinely in-scope.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


def is_excluded_by_cooccurrence(text: str | None) -> bool:
    """True if any exclusion pair's two patterns both match the same sentence."""
    if not text or not _EXCLUSION_PAIRS:
        return False
    for sentence in _SENTENCE_SPLIT_RE.split(str(text)):
        for re_a, re_b in _EXCLUSION_PAIRS:
            if re_a.search(sentence) and re_b.search(sentence):
                return True
    return False

# Canonical fields the DB knows about (everything except identity + lob_*)
CANONICAL_FIELDS = (
    "case_type", "open_date", "close_date", "activity_date", "activity",
    "inspector", "deputy_clerk", "permit_number", "building_code", "district_number",
    "property_address", "folio_number", "legal_description",
    "owner_full_name", "owner_mailing_address", "violator",
    "alleged_violation", "comments",
)

DATE_FIELDS = {"open_date", "close_date", "activity_date"}


def find_matched_keywords(text: str | None) -> list[str]:
    """Return the unique, lowercased keyword strings found in `text`."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return []
    hits = {m.group(0).lower() for m in _KEYWORD_RE.finditer(str(text))}
    return sorted(hits)


def clean(val: Any) -> str | None:
    """Normalize cell values: strip whitespace, drop NaN/sentinels."""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "&nbsp;", "\xa0", "none"}:
        return None
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_date(val: Any) -> str | None:
    """
    Convert various date representations to ISO 'YYYY-MM-DD'.
    Returns None for empty cells and the ASP.NET '1/1/0001' sentinel.
    """
    s = clean(val)
    if not s:
        return None
    try:
        dt = pd.to_datetime(s, errors="coerce")
        if pd.isna(dt) or dt.year < 1900:
            return None
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def build_record(
    *,
    source: str,
    case_number: str,
    fields: dict[str, Any],
    raw_source_file: str | None = None,
    keyword_search_field: str = "alleged_violation",
    skip_filter: bool = False,
) -> dict | None:
    """
    Build a canonical record dict ready for db.upsert_violations().

    `fields` is a dict mapping any of CANONICAL_FIELDS → raw value.
    Date fields are normalized; everything else is cleaned.
    The keyword filter runs against `fields[keyword_search_field]`.
    Returns None if the row doesn't match any keyword (and skip_filter is False).

    Pass skip_filter=True for sources that arrive already pre-filtered (e.g.
    a records-request export that the city already scoped to your trades).
    """
    case_number = clean(case_number)
    if not case_number:
        return None

    record: dict[str, Any] = {
        "source": source,
        "case_number": case_number,
        "raw_source_file": raw_source_file,
    }

    for field in CANONICAL_FIELDS:
        raw = fields.get(field)
        record[field] = normalize_date(raw) if field in DATE_FIELDS else clean(raw)

    matched = find_matched_keywords(record.get(keyword_search_field))
    if not matched and not skip_filter:
        return None
    # Co-occurrence exclusions apply only when the inclusion filter is
    # active. Pre-filtered uploads (records-request exports the operator
    # has already scoped) bypass both rules.
    if matched and is_excluded_by_cooccurrence(record.get(keyword_search_field)):
        return None
    record["matched_keywords"] = ",".join(matched) if matched else "(pre-filtered)"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record["first_seen_at"] = now
    record["last_seen_at"] = now

    return record
