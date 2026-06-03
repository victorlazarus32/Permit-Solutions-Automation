"""
Per-city configuration for Public Records Request (PRR) workflows.

This is the source of truth for "which portal do I submit to" and "who do I
email if they're slow" for every Miami-Dade city PSS pulls violations from
via PRR. Auto-fills the Log-PRR form on the /prr dashboard, populates the
default request-body template, and is the contact lookup the reminder
routines reference.

Add a new city by adding an entry to PRR_CITIES. The `source` key must
match the canonical source string used throughout the app (it's the same
key that appears in violations.source and in _PRR_CITIES on server.py).

PRR_CADENCE_DAYS is the policy lever for how often to re-submit each city's
PRR. Set in 2026-06-03 to 14 (biweekly) — Victor's call after the first
three cities were onboarded.
"""
from __future__ import annotations

from dataclasses import dataclass

PRR_CADENCE_DAYS = 14  # biweekly


@dataclass(frozen=True)
class PrrCity:
    source:            str
    pretty_name:       str
    portal_url:        str
    custodian_name:    str
    custodian_email:   str
    custodian_phone:   str
    custodian_address: str = ""
    # Per-city request-body template. Format with {start} and {end} (ISO dates)
    # and {requester_email} when rendering. Leave blank to use the default
    # template defined on /prr.
    request_template:  str = ""


PRR_CITIES: dict[str, PrrCity] = {
    "cutler_bay": PrrCity(
        source="cutler_bay",
        pretty_name="Town of Cutler Bay",
        portal_url="https://www.cutlerbay-fl.gov/townclerk/webform/public-records-request",
        custodian_name="Mauricio Melinu, CMC (Town Clerk)",
        custodian_email="MMelinu@cutlerbay-fl.gov",
        custodian_phone="(305) 234-4262",
        custodian_address=("Office of the Town Clerk, 10720 Caribbean Blvd, "
                           "Suite 105, Cutler Bay, FL 33189"),
    ),
    "palmetto_bay": PrrCity(
        source="palmetto_bay",
        pretty_name="Village of Palmetto Bay",
        portal_url="https://palmettobayfl.justfoia.com/publicportal/home/newrequest",
        custodian_name="Office of Public Records",
        custodian_email="publicrecords@palmettobay-fl.gov",
        custodian_phone="(305) 259-1234",
        custodian_address=("9705 East Hibiscus Street, Palmetto Bay, FL 33157 "
                           "— Monday–Friday, 8:30 AM – 5 PM"),
    ),
    "city_of_miami": PrrCity(
        source="city_of_miami",
        pretty_name="City of Miami",
        portal_url="https://miami.nextrequest.com/requests/new",
        custodian_name="City of Miami Public Records",
        custodian_email="PublicRecords@miamigov.com",
        custodian_phone="(305) 416-1883",
    ),
}


DEFAULT_REQUEST_TEMPLATE = """\
Records requested:
A list of all Code Compliance cases / Notices of Violation opened by the
{pretty_name} Code Compliance Division between {start} and {end},
inclusive.

For each case, please include (to the extent maintained):
  - Case number
  - Date opened
  - Property address
  - Folio number (if available)
  - Owner name (if available)
  - Violation type / category
  - Violation description
  - Current case status

Preferred format: Microsoft Excel (.xlsx) or CSV. Email delivery is fine.

Purpose: tracking open code-compliance cases for outreach to affected
property owners regarding permit assistance.

Contact: {requester_email}
"""


def render_request_body(city: PrrCity, *, start: str, end: str,
                        requester_email: str) -> str:
    """Render the per-city PRR request body. Falls back to the default."""
    template = city.request_template or DEFAULT_REQUEST_TEMPLATE
    return template.format(
        pretty_name=city.pretty_name,
        start=start,
        end=end,
        requester_email=requester_email,
    )
