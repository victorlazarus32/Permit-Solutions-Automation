---
name: intake
description: >-
  Capture, update, and review Victor's client / prospective-client intakes in
  the private local tracker. Use whenever he dumps info about a client or
  prospect, asks who to follow up with, wants a pipeline summary, or updates a
  job's status. Triggers: intake, new client, new prospect, lead, "add this
  client", follow up, pipeline, "who haven't I called", "what's open", quote
  status, won, lost.
---

# Intake tracker assistant

Victor uses this as a lightweight CRM until the app does it natively. He dumps
client/prospect info in chat (a name + number, a story, a forwarded text); you
keep it organized in the tracker and answer questions about the pipeline.

## The tracker file
`INTAKE_TRACKER.local.md` at the repo root.

- It is **gitignored and PRIVATE** — this repo is **public**. Put client PII
  ONLY in this file. **Never** commit it, never copy client names/phones/
  addresses into any tracked/committed file, and never paste PII into commit
  messages or pushed content.
- Read it before adding/updating so you don't duplicate a record.

## Record fields
Name · Phone/Email · Property address · Jurisdiction · Source (how they found
us) · Violation/Case # · Scope/trades · Status · Next action (+ due date) ·
Notes. Status flow: `New → Contacted → Quoted → Won` (or `Lost`).

## Adding an intake
1. Parse whatever Victor gives into the fields above. Leave unknowns blank and
   ask only for what materially matters (at minimum a name or a way to identify
   them).
2. Default **Status = New** unless he says otherwise. Always set a concrete
   **Next action** with a due date (convert relative dates like "next week" to
   an absolute date; today's date is in the session context).
3. Prepend the record to the `## Records` section (newest first), and update
   the `## Open pipeline summary` table (drop Won/Lost from the open summary).
4. Confirm back in one line what you logged and the next action.

Record format:
```
### [STATUS] Name — short tag
- **Phone/Email:** …
- **Property:** …
- **Jurisdiction:** …
- **Source:** …
- **Violation/Case #:** …
- **Scope/trades:** …
- **Status:** New
- **Next action:** … (due YYYY-MM-DD)
- **Notes:** …
- _Added: YYYY-MM-DD · Updated: YYYY-MM-DD_
```

## Updating
Find the record by name (fuzzy ok). Update fields/status/next action, bump the
`Updated:` date, and keep the summary table in sync. When a job becomes a real
engagement, note its linked invoice number (PSS-…) if there is one.

## Reviewing / recall
Answer questions from the file: open pipeline, who's stale (next action past
due or no contact in a while), hot leads (Quoted), this week's intakes, status
of a named client. Be proactive: when asked "what's open," flag overdue
next-actions first.

## Don't
- Don't commit the tracker or its contents.
- Don't invent details — only record what Victor provides.
