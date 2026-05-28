# Permit Solutions Automation — Architecture

The app behind **app.permitsolutions.us**. It pulls code-violation records from
city/county portals, filters them to the kinds of work we can help with, enriches
them with property-owner data, and mails letters to the owners via Lob.

This document maps the full pipeline so a developer (or an AI agent) can find any
stage without spelunking.

---

## The pipeline at a glance

```
  DOWNLOAD            FILTER              SAVE                ENRICH               MAIL
 (connectors)  →   (keyword match)  →  (upsert to DB)  →  (owner lookup)  →   (Lob letters)
```

| Stage    | What happens                                              | Where                                  |
|----------|-----------------------------------------------------------|----------------------------------------|
| Download | Fetch raw violation rows from each city/county source     | `connectors/*.py`                      |
| Filter   | Keep only rows whose text matches our trade keywords      | `config/keywords.py`                   |
| Save     | Insert/update rows in the `violations` table              | `db.py` → `upsert_violations()`        |
| Enrich   | Look up the property owner + mailing address              | `lookup/property_appraiser.py`         |
| Verify   | Pre-flight check the mailing address with Lob             | `lob_sender/verify.py`                 |
| Mail     | Send the letter via Lob, track delivery                   | `lob_sender/send.py`, `webhook.py`     |

---

## 1. Download — the connectors

One connector per source. Each exposes a `run()` function that downloads,
filters, and saves in a single call.

- **Homestead** — [connectors/tyler_energov.py](connectors/tyler_energov.py)
  Hits the Tyler EnerGov public JSON API over HTTPS.
  - Download: `fetch_pages()` — [tyler_energov.py:266](connectors/tyler_energov.py#L266)
  - Orchestration: `run("homestead")` — [tyler_energov.py:341](connectors/tyler_energov.py#L341)
  - Uses a watermark file so each run only pulls cases newer than the last.

- **Miami-Dade Unincorporated** — [connectors/miami_dade_unincorporated.py](connectors/miami_dade_unincorporated.py)
  Selenium/Chromium scraper (the portal has no API).
  - Download: `fetch_export()` — [miami_dade_unincorporated.py:100](connectors/miami_dade_unincorporated.py#L100)
  - Orchestration: `run()` — [miami_dade_unincorporated.py:185](connectors/miami_dade_unincorporated.py#L185)

- **Manual PRR uploads** — `connectors/homestead.py`, `palmetto_bay.py`,
  `cutler_bay.py`, `pinecrest.py`, `miami_beach.py`. These ingest spreadsheet
  files an operator uploads via the dashboard, for cities that only release
  records through public-records requests.

## 2. Filter — keyword matching

[config/keywords.py](config/keywords.py) holds the word-boundary regex patterns
(fence, gate, roof, electrical, etc.). Each connector's `run()` applies the
filter before saving, so off-topic violations never enter the database.

## 3. Save — the one chokepoint

**Every** connector funnels into a single function:

- [db.py:492](db.py#L492) → `upsert_violations(rows)`

It inserts or updates rows in the `violations` table, keyed on
`(source, case_number)`. Re-running a connector refreshes existing rows instead
of duplicating them. It never touches the `lob_*` mailing fields — those are
owned by the mail stage.

- Table schema: `violations` in the `SCHEMA` constant, [db.py:27](db.py#L27)
- DB file: `data/violations.db` locally, `/var/data/violations.db` on Render
  (persistent disk). Path is overridable via the `DB_PATH` env var.

## 4. Enrich — owner lookup

Tyler's search endpoint doesn't carry owner names, so newly-saved rows are
flagged `NEEDS_OWNER_LOOKUP` and resolved against the Miami-Dade Property
Appraiser in the same run.

- [lookup/property_appraiser.py](lookup/property_appraiser.py) — `lookup()`
  (folio → owner) and `search_by_address()` (used by the dashboard search bar).

## 5. Verify + Mail — Lob

- **Verify** — [lob_sender/verify.py](lob_sender/verify.py) → `verify_us_address()`.
  Pre-flight US address check (~$0.0075) so we don't waste ~$1.50 on a letter
  USPS would return. Runs automatically inside the send loop; undeliverable rows
  are skipped.
- **Send** — [lob_sender/send.py](lob_sender/send.py) → `send_batch()`. Builds
  the letter payload and POSTs to the Lob Letters API. Writes the returned
  letter ID + status back onto the violation row.
- **Address derivation** — [lob_sender/derive.py](lob_sender/derive.py) turns a
  messy DB row into clean Lob fields (name parsing, address splitting,
  bilingual merge variables).
- **Delivery tracking** — [lob_sender/webhook.py](lob_sender/webhook.py) receives
  Lob delivery-status webhooks and updates `lob_status` / `lob_delivered_at`.

---

## Automation & web layer

- **Daily cron** — [scripts/daily_run.py](scripts/daily_run.py). Render Cron Job
  runs this once a day: pulls both sources, optionally auto-sends letters
  (gated on `DAILY_AUTO_SEND=1`), and writes an audit row to the `daily_runs`
  table viewable on `/reports`.
- **Web app** — [app/server.py](app/server.py). Flask app: dashboard, letter
  queue, sent letters, reports, invoices/workflow, settings. Templates in
  `app/templates/`.

## Key environment variables

| Var                    | Purpose                                              |
|------------------------|------------------------------------------------------|
| `DB_PATH`              | SQLite location (set to the Render disk in prod)     |
| `DATA_DIR`             | Where connector watermarks persist                   |
| `LOB_API_KEY`          | Lob secret key (letters + address verification)      |
| `LOB_TEMPLATE_ID`      | Lob letter template                                  |
| `LOB_FROM_ADDRESS_ID`  | Return address for every letter                      |
| `LOB_VERIFY_ADDRESSES` | Pre-flight verification on/off (default on)          |
| `DAILY_AUTO_SEND`      | Let the daily cron mail unattended (default off)     |
| `DAILY_SEND_SINCE_DAYS`| How fresh a case must be for the cron to mail it     |
