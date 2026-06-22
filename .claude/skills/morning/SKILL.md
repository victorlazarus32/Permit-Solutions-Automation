---
name: morning
description: >-
  Produce Victor's morning report / daily brief — read the intake tracker and
  surface everything that needs attention today: overdue and due-today
  follow-ups first, approaching county deadlines, hot leads, and active jobs.
  Use for "morning report", "daily report", "/morning", "what's on my plate",
  "what's due today", "give me my brief", "catch me up", "what do I owe people".
---

# Morning report / daily brief

Victor wants one prioritized brief of all his intakes, reminders, and
follow-ups so nothing slips. Source of truth is the private local tracker.

## Source
`INTAKE_TRACKER.local.md` (repo root — gitignored, contains client PII, never
commit). Read it first. Use today's date from the session context for all
date comparisons.

## Produce — tight and scannable, in this order
1. **🔴 Overdue / due today** — every record whose Next-action due date is on or
   before today. For each: name · the one action to take · how overdue · phone.
   This leads the report.
2. **⏳ Deadlines approaching** — any record with a county/compliance deadline
   within ~14 days; show the date and days remaining.
3. **🔥 Hot leads** — anything in **Quoting/Quoted** (a number is out, awaiting a
   yes) — nudge him to chase, oldest first.
4. **Pipeline at a glance** — counts by status (New / Contacted / Quoting /
   Quoted / Won / Lost) + any in-progress jobs with their stage.
5. **🏦 Personal / admin reminders** — surface any items from the tracker's
   `## Personal / Admin reminders` section that are due on/before today (bank,
   bills, errands). These are non-client but Victor still wants them in the brief.
6. If nothing is overdue or due today, say so plainly.

End with a one-line **"Start here today: …"** recommendation (the single
highest-value action).

## Rules
- Names + the one thing to do — not a data dump.
- Only what's in the tracker; never invent records.
- Read + display only; never write PII into anything committed.
- If he asks, also list active scheduled cloud reminders via RemoteTrigger
  (action: list) and fold their fire times into the brief.
