# Permit Solutions — Violation Pipeline

End-to-end automation: pull code-enforcement violations from municipal sources →
filter to your trade scope → mail notice letters via Lob → track delivery
status in one database.

## Pipeline stages

```
[1] Ingestion        →  [2] Normalization  →  [3] Letter generation  →  [4] Lob send  →  [5] Delivery tracking
    (per-city                (one DB)            (HTML template +       (Letters API     (webhook receiver
     connector)                                   merge variables)       w/ idempotency)  → DB updates)
```

## Project layout

```
permit_solutions/
├── config/
│   └── keywords.py              ← edit to tune the trade-scope filter
├── connectors/                  ← one module per municipality
│   ├── miami_dade_unincorporated.py    (scheduled — Playwright scraper)
│   └── homestead.py                    (manual upload — inbox folder)
├── lob_sender/                  ← stages 3 & 4
│   ├── derive.py                       (DB row → merge variables + parsed address)
│   └── send.py                         (CLI to mail eligible rows via Lob)
├── webhook/                     ← stage 5
│   └── server.py                       (Flask receiver for Lob delivery events)
├── templates/
│   └── violation_letter_en.html        (the letter, with {{merge_variables}})
├── scripts/
│   └── upload_template.py              (one-time: push template to Lob)
├── tests/
│   └── test_derive.py                  (22 unit tests)
├── data/
│   ├── violations.db                   (SQLite — created on first run)
│   └── *_last_run.txt                  (per-connector watermark)
├── audit/                              (raw scraped files)
│   └── miami_dade_unincorporated/
├── inbox/                              (drop-zone for manual uploads)
│   ├── homestead/
│   └── processed/
│       └── homestead/
├── db.py                        ← shared SQLite schema + upsert helpers
├── parser.py                    ← shared cleaning + keyword filter
├── .env.example                 ← copy to .env, fill in your Lob keys
└── README.md
```

## Setup

```bash
cd permit_solutions
python -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install playwright pandas openpyxl lxml html5lib flask
playwright install chromium                # one-time browser download
python db.py                               # create the SQLite tables
cp .env.example .env                       # then edit .env with your Lob keys
```

---

## Stage 1 — Ingestion

Two patterns covered. Add new municipalities by copying whichever fits.

### Scheduled scraper (Miami-Dade Unincorporated)

```bash
# Specific date range
python -m connectors.miami_dade_unincorporated --start 2026-04-15 --end 2026-04-22

# Default range (since last successful run)
python -m connectors.miami_dade_unincorporated

# Watch the browser do its thing (debugging)
python -m connectors.miami_dade_unincorporated --show-browser
```

After a run: raw export archived to `audit/`, matched cases upserted, watermark
saved.

### Manual upload (Homestead)

```bash
cp /path/to/Homestead_PRR_*.xlsx  inbox/homestead/
python -m connectors.homestead
```

Files auto-archive to `inbox/processed/homestead/<timestamp>__<filename>`.
Rows missing a mailing address are tagged `NEEDS_OWNER_LOOKUP` and held back
from mailing.

---

## Stage 2 — Filtering

Edit `config/keywords.py`. Each entry is a regex applied (case-insensitive) to
the violation description. Preview a match without writing to the DB:

```python
from parser import find_matched_keywords
find_matched_keywords("Durafence gates, front door replaced with impact door.")
# → ['door', 'durafence', 'gates']
```

The Homestead connector skips this filter because the source PRR is already
pre-scoped. To enforce it for that source, change `skip_filter=True` to
`skip_filter=False` in `connectors/homestead.py`.

---

## Stages 3 & 4 — Letter generation + Lob send

### One-time setup (do these in order)

1. **Get a Lob API key** — sign up at https://dashboard.lob.com, copy your
   test key (`test_*`) from Settings → API Keys. Paste into `.env` as
   `LOB_API_KEY`.
2. **Create your return address in the Lob dashboard** — Address Book → Add.
   Copy the `adr_xxx` ID into `.env` as `LOB_FROM_ADDRESS_ID`.
3. **Upload the letter template:**
   ```bash
   python -m scripts.upload_template
   ```
   Paste the returned `tmpl_xxx` ID into `.env` as `LOB_TEMPLATE_ID`. After
   this, you can edit the template directly in the Lob dashboard without
   re-uploading.

### Daily use

```bash
# Preview what would be sent — no API call, no charge
python -m lob_sender.send --dry-run --limit 3

# Send 1 real letter (test mode if your key is test_*)
python -m lob_sender.send --limit 1

# Send everything eligible (use carefully in live mode!)
python -m lob_sender.send
```

Behavior:
- Only mails rows that have a parseable mailing address, an owner name, are
  not flagged `NEEDS_OWNER_LOOKUP`, and have not been mailed yet
  (`lob_letter_id IS NULL`).
- Every send uses an **idempotency key** derived from `(source, case_number)`,
  so a retry within 24 hours produces the same letter, not a duplicate.
- On success, writes back `lob_letter_id`, `lob_status`, `lob_mailed_at`.
- On failure, logs the Lob error response and continues with the next row.

Letter content per row, derived automatically:
- **Salutation** — first name from `owner_full_name`, falls back to "Property
  Owner" for LLCs, trusts, or multi-owner deeds.
- **Mailing recipient** — first owner only when there are multiple (Lob caps
  the name field at 40 chars; truncating mid-name looks worse than dropping
  the second owner).
- **Violation subject** — derived from `matched_keywords`, smart-joined when
  multiple ("fence, gate, and electrical work").
- **Jurisdiction** — looked up from the source via
  `JURISDICTION_BY_SOURCE` in `lob_sender/derive.py` (add a line per new
  municipality).

---

## Stage 5 — Delivery tracking

Lob fires webhooks for every status transition (mailed → in-transit → delivered
→ returned). The included Flask receiver writes those events back to the DB
keyed on the `case_number` you stashed in the letter's `metadata`.

### Run locally for testing

```bash
pip install flask
python -m webhook.server
```

Listens on `http://0.0.0.0:5000/lob-webhook`. To get Lob to reach this from
the public internet, use [ngrok](https://ngrok.com):

```bash
ngrok http 5000
# Copy the HTTPS URL it prints, then add to Lob dashboard:
#   Settings → Webhooks → Add Endpoint → paste the URL + "/lob-webhook"
```

### Production deployment

Deploy the same `webhook.server` module under any WSGI host (gunicorn,
uvicorn+ASGI shim, etc.). Set `LOB_WEBHOOK_SECRET` in the environment so the
receiver can verify the `Lob-Signature` header on each incoming request.

---

## Inspecting the database

```bash
sqlite3 data/violations.db
```

```sql
-- Counts by source
SELECT source, COUNT(*) FROM violations GROUP BY source;

-- Ready to mail
SELECT source, case_number, owner_full_name, owner_mailing_address
FROM violations
WHERE owner_mailing_address IS NOT NULL
  AND lob_letter_id IS NULL
  AND (comments NOT LIKE '%NEEDS_OWNER_LOOKUP%' OR comments IS NULL);

-- In-flight letters
SELECT source, case_number, lob_letter_id, lob_status, lob_mailed_at
FROM violations
WHERE lob_letter_id IS NOT NULL
ORDER BY lob_last_event_at DESC;

-- Returned mail (need owner address research)
SELECT source, case_number, owner_full_name, owner_mailing_address, lob_status
FROM violations
WHERE lob_status = 'returned_to_sender';
```

---

## Adding a new municipality

### Scrapeable
1. Copy `connectors/miami_dade_unincorporated.py` → `connectors/<city>.py`
2. Update `SOURCE`, `URL`, `COLUMN_MAP`, and the Playwright form steps
3. Add the city's pretty name to `JURISDICTION_BY_SOURCE` in `lob_sender/derive.py`
4. Confirm the keyword filter catches the right cases on a sample export

### Manual upload
1. Copy `connectors/homestead.py` → `connectors/<city>.py`
2. Update `SOURCE`, `HEADER_ROW`, and `COLUMN_MAP` to match the file format
3. Add city-specific cleaning (mailing-address combining, etc.) if needed
4. Add to `JURISDICTION_BY_SOURCE` in `lob_sender/derive.py`
5. Create `inbox/<city>/` and start dropping files

The DB schema, keyword filter, letter template, Lob sender, and webhook
receiver are all reused for free.

---

## Running the test suite

```bash
python -m unittest discover tests
```

Currently 22 tests covering name extraction, address parsing, subject
derivation, and end-to-end row → Lob payload conversion.

---

## Scheduling

Pick one for the scrapers:

- **Cron** (Linux/Mac):
  ```
  0 7  * * *   cd /path/to/permit_solutions && .venv/bin/python -m connectors.miami_dade_unincorporated >> logs/cron.log 2>&1
  30 7 * * *   cd /path/to/permit_solutions && .venv/bin/python -m lob_sender.send --limit 50         >> logs/cron.log 2>&1
  ```
- **Task Scheduler** (Windows): point at `python.exe` with the same args
- **APScheduler** (in-process): wrap `run()` in a scheduled job inside a
  long-running app

Manual-upload sources don't need scheduling.
