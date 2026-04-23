"""
Convert a violation DB row into Lob-ready inputs.

Two outputs per row:
  1. `merge_variables` dict  → goes into the Lob letter request `merge_variables`
  2. `to_address` dict       → goes into the Lob letter request `to`

This module contains all the heuristics for turning messy real-world data
(LLC names, multi-owner deeds, abbreviated streets, free-form mailing
addresses) into the clean fields Lob expects. Test thoroughly when adding new
sources — the address parser in particular tends to need adjustment per city.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

# Sources we know how to introduce in the letter copy.
JURISDICTION_BY_SOURCE = {
    "miami_dade_unincorporated": "Miami-Dade County",
    "homestead": "the City of Homestead",
    # Add new connectors here as you onboard them.
}

# Map keyword hits → human-friendly noun used in the letter sentence
# "...may have a violation concerning your {violation_subject}".
# Order matters: more specific terms first.
_KEYWORD_TO_SUBJECT = [
    (re.compile(r"\bdurafence\b", re.I),                "fence"),
    (re.compile(r"\bchain[- ]?link( fence)?\b", re.I),  "fence"),
    (re.compile(r"\bmetal fence\b", re.I),              "fence"),
    (re.compile(r"\bfence(s|ing)?\b", re.I),            "fence"),
    (re.compile(r"\bgate(s)?\b", re.I),                 "gate"),
    (re.compile(r"\bgarage door(s)?\b", re.I),          "garage door"),
    (re.compile(r"\bdoor(s)?\b", re.I),                 "door"),
    (re.compile(r"\bwindow(s)?\b", re.I),               "window"),
    (re.compile(r"\belectrical\b", re.I),               "electrical work"),
]

# Generic / corporate name fragments — when we see these in owner_full_name,
# we don't try to extract a "first name" for the salutation.
_NON_PERSONAL_TOKENS = re.compile(
    r"\b(LLC|L\.L\.C\.?|INC|CORP|CORPORATION|CO|TRUST|TR|LP|LLP|LTD|"
    r"PROPERTIES|HOLDINGS|ASSOC(IATION)?|HOA|FOUNDATION|ESTATE|"
    r"BANK|REALTY|MANAGEMENT|GROUP|ENTERPRISES|PARTNERS|INVESTMENTS)\b",
    re.I,
)

GENERIC_SALUTATION = "Property Owner"


# ---------- Derived fields ----------

def derive_violation_subject(matched_keywords: str | None,
                             alleged_violation: str | None) -> str:
    """
    Build the noun phrase that goes after "concerning your ___".
    Searches matched_keywords first (precise), then falls back to the full
    alleged_violation text. Combines multiple subjects with " and ".
    """
    haystack_parts = [matched_keywords or "", alleged_violation or ""]
    haystack = " ".join(haystack_parts)
    if not haystack.strip():
        return "property"

    found: list[str] = []
    for pattern, subject in _KEYWORD_TO_SUBJECT:
        if pattern.search(haystack) and subject not in found:
            found.append(subject)

    if not found:
        return "property"
    if len(found) == 1:
        return found[0]
    if len(found) == 2:
        return f"{found[0]} and {found[1]}"
    return ", ".join(found[:-1]) + f", and {found[-1]}"


def derive_first_name(owner_full_name: str | None) -> str:
    """
    Pull a friendly salutation token from the owner name field.
    Returns 'Property Owner' for LLCs, trusts, multi-owner deeds, or anything
    we can't confidently address by first name.
    """
    if not owner_full_name:
        return GENERIC_SALUTATION

    name = owner_full_name.strip()

    # Multi-owner deeds usually contain '/', ',', '&' or ' AND '
    # Miami-Dade uses ',' (e.g. "MORITZ ESSER, YUDIT VIRGINIA PINA RODRIGUEZ")
    # Homestead uses '/' (e.g. "Yamil Horruitinel / Andres F Salazar")
    if "/" in name or "," in name or "&" in name or " AND " in name.upper():
        return GENERIC_SALUTATION

    # Corporate / non-person owners
    if _NON_PERSONAL_TOKENS.search(name):
        return GENERIC_SALUTATION

    # Take the first whitespace-separated token, strip trailing punctuation
    first_token = name.split()[0].strip(",.;:")
    if not first_token:
        return GENERIC_SALUTATION
    return first_token


def format_letter_date(d: date | None = None) -> str:
    """e.g. 'April 22, 2026'  (no leading zero on the day on most platforms)."""
    d = d or date.today()
    # %-d is not portable to Windows; build it manually for safety
    return f"{d.strftime('%B')} {d.day}, {d.year}"


# ---------- Address parsing ----------

# Match a US ZIP code at the end of a string: 5 or ZIP+4
_ZIP_RE = re.compile(r"\b(\d{5}(?:-\d{4})?)\s*$")
# State is 2 uppercase letters before the ZIP
_STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


def parse_mailing_address(raw: str | None) -> dict[str, str] | None:
    """
    Parse a free-form mailing address string into Lob's required fields.
    Returns a dict with address_line1, address_line2, address_city,
    address_state, address_zip — or None if the string is unparseable.

    Handles common shapes seen in our two source files:
      "8101 SW 72ND AVE 404W , MIAMI FL 33143"
      "1828 Se 18 Ter, Homestead FL 33035"
      "11070 NW 22ND CT , MIAMI FL 33167-3053"
      "4954 SW 186TH WAY , MIRAMAR FL 33029-6240"
    """
    if not raw:
        return None
    s = re.sub(r"\s+", " ", raw.strip()).rstrip(",")

    # Find "STATE ZIP" at the end
    m = _STATE_ZIP_RE.search(s)
    if not m:
        log.warning("Could not parse state/zip from address: %r", raw)
        return None

    state = m.group(1)
    zipc  = m.group(2)
    head  = s[: m.start()].strip().rstrip(",")  # everything before "STATE ZIP"

    # Now `head` looks like "STREET, CITY"  (city is whatever follows the last comma)
    if "," in head:
        street, city = head.rsplit(",", 1)
        street = street.strip().rstrip(",")
        city = city.strip()
    else:
        # No comma — city is the last whitespace-separated token; everything
        # else is the street. This is fragile but covers the few sources that
        # omit the comma.
        toks = head.split()
        if len(toks) < 2:
            log.warning("Address too short to split city: %r", raw)
            return None
        street = " ".join(toks[:-1]).strip()
        city = toks[-1].strip()

    if not street or not city:
        log.warning("Empty street or city after parse: %r", raw)
        return None

    # Lob caps address_line1 at 64 chars. Try to split overflow into line2.
    line1, line2 = _split_line1(street)

    return {
        "address_line1": line1,
        "address_line2": line2,
        "address_city":  city.upper(),
        "address_state": state.upper(),
        "address_zip":   zipc,
    }


def _split_line1(street: str) -> tuple[str, str]:
    """
    Lob's address_line1 has a 64-char limit. If the street is longer, look for
    a unit/apt/suite token to split on; otherwise hard-truncate.
    """
    if len(street) <= 64:
        return street, ""

    # Common unit markers
    m = re.search(
        r"\b(APT|APARTMENT|UNIT|STE|SUITE|#|BLDG|BUILDING|LOT|FL|FLOOR)\b",
        street, re.I,
    )
    if m and m.start() <= 64:
        line1 = street[: m.start()].strip().rstrip(",")
        line2 = street[m.start():].strip()
        return line1[:64], line2[:64]

    # Last resort: hard cut
    return street[:64], street[64:128]


# ---------- Top-level ----------

def derive_for_row(row: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """
    Take a violations DB row and return a dict with two keys:
      - 'merge_variables': dict to pass to Lob's merge_variables
      - 'to_address':      dict to pass to Lob's `to` field
      - 'errors':          list of issues (empty = ready to send)
    """
    errors: list[str] = []

    # Mailing address — fail loudly if unparseable
    to_address = parse_mailing_address(row.get("owner_mailing_address"))
    if not to_address:
        errors.append("unparseable_mailing_address")

    # Owner name is required by Lob (or 'company')
    raw_owner_name = (row.get("owner_full_name") or "").strip()
    if not raw_owner_name:
        errors.append("missing_owner_name")
        owner_name = ""
    else:
        # Multi-owner records (e.g. "MORITZ ESSER, YUDIT VIRGINIA PINA RODRIGUEZ"
        # or "Joaquin Diaz / Cledy Velasquez") often blow past Lob's 40-char
        # name limit. Mail to the FIRST owner only — cleaner than truncating
        # mid-word, and the property address is the same regardless.
        owner_name = re.split(r"\s*[,/]\s*", raw_owner_name, maxsplit=1)[0].strip()
        if len(owner_name) > 40:
            log.warning("Truncating single-owner name >40 chars: %r", owner_name)
            owner_name = owner_name[:40].rstrip()

    if to_address:
        to_address = {"name": owner_name, **to_address}

    # Jurisdiction (used in letter copy)
    jurisdiction = JURISDICTION_BY_SOURCE.get(
        row.get("source", ""), "your local jurisdiction"
    )

    merge = {
        "date":                 format_letter_date(today),
        "owner_name":           owner_name,  # cleaned, matches `to.name`
        "owner_address_line1":  (row.get("owner_mailing_address") or "").strip(),
        "case_number":          str(row.get("case_number") or ""),
        "first_name":           derive_first_name(row.get("owner_full_name")),
        "property_address":     (row.get("property_address") or "").strip(),
        "violation_subject":    derive_violation_subject(
                                    row.get("matched_keywords"),
                                    row.get("alleged_violation"),
                                ),
        "jurisdiction":         jurisdiction,
    }

    return {
        "merge_variables": merge,
        "to_address":      to_address,
        "errors":          errors,
    }
