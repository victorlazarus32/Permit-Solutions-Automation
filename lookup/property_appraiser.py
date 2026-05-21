"""
Miami-Dade Property Appraiser folio -> owner lookup.

Public, anonymous JSON endpoint. Used to enrich Tyler-sourced rows (which
don't carry owner data) and any other folio-only source we add.

Endpoint: GET https://apps.miamidadepa.gov/PApublicServiceProxy/PaServicesProxy.ashx
Params:   Operation=GetPropertySearchByFolio
          folioNumber=<13-digit folio, no dashes>
          clientAppName=PropertySearch

Response shape we care about:
  OwnerInfos[].Name           -> owner name parts (joined)
  MailingAddress.Address1     -> street
  MailingAddress.Address2/3   -> suite/floor (rarely populated)
  MailingAddress.City         -> city
  MailingAddress.State        -> state code
  MailingAddress.ZipCode      -> zip+4 or zip5

Verified live 2026-05-03 against canary folio 1078130040420 (Homestead).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

ENDPOINT = "https://apps.miamidadepa.gov/PApublicServiceProxy/PaServicesProxy.ashx"
OPERATION = "GetPropertySearchByFolio"
CLIENT_APP = "PropertySearch"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3                # total attempts on transient errors
RETRY_BACKOFF_BASE_SEC = 2.0   # 2s, 4s, 8s between attempts

# Polite spacing between calls when batch-enriching. The PA service is robust
# but there's no reason to be inconsiderate.
PER_LOOKUP_SLEEP = 0.25

log = logging.getLogger("property_appraiser")


@dataclass
class OwnerInfo:
    """Result of one folio lookup. All fields may be empty strings on miss."""
    folio:                 str
    owner_full_name:       str    # OwnerInfos[].Name joined with " "
    owner_mailing_address: str    # one-line "Street, City State Zip" string
    site_address:          str    # primary SiteAddress
    raw:                   dict   # the full response, for debugging

    def found(self) -> bool:
        return bool(self.owner_full_name and self.owner_mailing_address)


def _normalize_folio(folio: str) -> str:
    """Strip dashes, dots, and whitespace so '10-7813-004-0420' -> '1078130040420'."""
    return "".join(ch for ch in str(folio).strip() if ch.isdigit())


def _join_owner_names(owner_infos: list[dict]) -> str:
    """
    Owner names on the PA roll are split across multiple OwnerInfos entries
    when the legal name is long. e.g. ["PALMETTO HOMES URBAN",
    "DEVELOPMENT GROUP INC"] -> "PALMETTO HOMES URBAN DEVELOPMENT GROUP INC".
    Empty/None entries are dropped.
    """
    parts = []
    for o in owner_infos or []:
        n = (o.get("Name") or "").strip()
        if n:
            parts.append(n)
    return " ".join(parts)


def _build_mailing_string(m: dict | None) -> str:
    """Format the MailingAddress object into 'Street, City State Zip'."""
    if not m:
        return ""
    a1 = (m.get("Address1") or "").strip()
    a2 = (m.get("Address2") or "").strip()
    a3 = (m.get("Address3") or "").strip()
    city = (m.get("City") or "").strip()
    state = (m.get("State") or "").strip()
    zipc = (m.get("ZipCode") or "").strip()

    if not a1:
        return ""

    street_parts = [p for p in (a1, a2, a3) if p]
    # Standard mailing format is "CITY, STATE ZIP" — the comma between city and
    # state is the convention readers expect on an envelope.
    if city and (state or zipc):
        locality = f"{city}, " + " ".join(p for p in (state, zipc) if p)
    else:
        locality = " ".join(p for p in (city, state, zipc) if p)
    if locality:
        return ", ".join([" ".join(street_parts), locality])
    return " ".join(street_parts)


def _primary_site_address(site_addrs: list[dict] | None) -> str:
    if not site_addrs:
        return ""
    a = site_addrs[0]
    return (a.get("Address") or "").strip()


def lookup(folio: str, *, session: requests.Session | None = None) -> OwnerInfo:
    """
    One-shot folio lookup. Returns an OwnerInfo with empty fields on miss.
    Raises on transport errors.
    """
    f = _normalize_folio(folio)
    if not f:
        return OwnerInfo(folio="", owner_full_name="", owner_mailing_address="",
                         site_address="", raw={})

    s = session or requests
    params = {"Operation": OPERATION, "folioNumber": f, "clientAppName": CLIENT_APP}
    headers = {"accept": "application/json", "user-agent": USER_AGENT,
               "referer": "https://www.miamidade.gov/Apps/PA/PropertySearch/"}

    # Retry on transient failures: connection resets, timeouts, and 5xx errors.
    # Miami-Dade PA flakes intermittently (especially from non-FL IPs like cloud
    # hosts). A few backoff retries turn most "PA down" runs into successes.
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = s.get(ENDPOINT, params=params, headers=headers,
                      timeout=REQUEST_TIMEOUT)
            # Retry on 5xx; let 4xx propagate immediately (client errors).
            if 500 <= r.status_code < 600:
                raise requests.HTTPError(f"{r.status_code} Server Error", response=r)
            r.raise_for_status()
            data = r.json()
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_err = e
            if attempt == MAX_RETRIES:
                raise
            sleep_for = RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            log.warning("PA lookup attempt %d/%d for folio %s failed (%s); "
                        "retrying in %.1fs",
                        attempt, MAX_RETRIES, f, e, sleep_for)
            time.sleep(sleep_for)
    else:
        raise RuntimeError(f"PA lookup exhausted retries: {last_err}")

    name = _join_owner_names(data.get("OwnerInfos") or [])
    mail = _build_mailing_string(data.get("MailingAddress"))
    site = _primary_site_address(data.get("SiteAddress"))

    return OwnerInfo(
        folio=f,
        owner_full_name=name,
        owner_mailing_address=mail,
        site_address=site,
        raw=data,
    )


def batch_lookup(folios, *, sleep_sec: float = PER_LOOKUP_SLEEP):
    """
    Generator that yields OwnerInfo for each input folio with a small sleep
    between calls. Use this for backfill scripts so the PA service isn't
    hammered.
    """
    with requests.Session() as session:
        for i, folio in enumerate(folios):
            try:
                yield lookup(folio, session=session)
            except Exception as e:
                log.warning("PA lookup failed for %s: %s", folio, e)
                yield OwnerInfo(folio=str(folio), owner_full_name="",
                                owner_mailing_address="", site_address="",
                                raw={"error": str(e)})
            if sleep_sec:
                time.sleep(sleep_sec)


def search_by_address(query: str, limit: int = 8) -> list[dict]:
    """
    Quick partial-address search against the PA. Returns up to `limit`
    candidate properties as dicts with normalized fields. Designed for the
    universal search dropdown — short timeout, never raises (returns []
    on any failure).

    Each result dict:
        {folio, owner_full_name, site_address, municipality, neighborhood}

    `query` is interpreted as best-effort: leading digits become the
    streetnumber param, the rest becomes streetname. Empty or too-short
    queries return [] without calling the API.
    """
    q = (query or "").strip()
    if len(q) < 4:
        return []

    # Split "11769 SW 222ND ST" -> streetnumber="11769", streetname="SW 222ND ST"
    parts = q.split(maxsplit=1)
    if not parts:
        return []
    streetnumber = ""
    streetname = q
    if parts[0].isdigit():
        streetnumber = parts[0]
        streetname = parts[1] if len(parts) > 1 else ""

    try:
        r = requests.get(
            ENDPOINT,
            params={
                "Operation":      "GetPropertySearchByAddress",
                "streetnumber":   streetnumber,
                "streetname":     streetname,
                "clientAppName":  CLIENT_APP,
            },
            headers={"accept": "application/json", "user-agent": USER_AGENT},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("PA address search failed for %r: %s", query, e)
        return []

    out: list[dict] = []
    for info in (data.get("MinimumPropertyInfos") or [])[:limit]:
        owner = " ".join(p for p in (info.get("Owner1"), info.get("Owner2"),
                                     info.get("Owner3")) if (p or "").strip())
        strap = (info.get("Strap") or "").replace("-", "")
        out.append({
            "folio":           strap,
            "owner_full_name": owner.strip(),
            "site_address":    (info.get("SiteAddress") or "").strip(),
            "site_unit":       (info.get("SiteUnit") or "").strip(),
            "municipality":    (info.get("Municipality") or "").strip(),
            "neighborhood":    (info.get("NeighborhoodDescription") or "").strip(),
        })
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")
    for f in sys.argv[1:] or ["1078130040420"]:
        info = lookup(f)
        print(f"\nFolio {info.folio}")
        print(f"  Owner:   {info.owner_full_name!r}")
        print(f"  Mailing: {info.owner_mailing_address!r}")
        print(f"  Site:    {info.site_address!r}")
