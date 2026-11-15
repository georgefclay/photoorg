# Phase 16 — Desktop UI rework (backlog; the prompt is not written yet)

The phase: **rebuild the desktop shell around how George actually uses it;
the engine stays** — repos, jobs, sync, migrations and both test suites
untouched. The build prompt gets written once George's full friction list is
in; until then this file collects the material. (PM, 2026-11-10)

## Agreed direction (from the 2026-11-06 discussion)
- Navigation by task, not build history: the finished one-time modes
  (Triage, Dedupe, Cleanup, back pairing) retire into a Tools menu; the
  daily screens (Suggestions, Corrections, Groups, Ingest, Sync) lead.
- A home screen with the live numbers — unfiled count, pending suggestions,
  writer due, last push/pull — each one a button to the screen that handles
  it.
- Standing shell rules, learned the hard way in the Nov fix-ups: never a
  silent zero (say what was searched); preview before apply; results are
  sentences, never dicts; a control never disappears to mean "nothing to
  do".

## Collected items
1. **The whole of Phase 9 fix-up 4** (deferred here 2026-11-10; full spec in
   `prompts/phase-09-web-core-sync.md`): one "Pull from the site" button
   with the three stage pulls under Advanced; per-kind plain-language pull
   and push summaries (needs `pull_confirmed`'s per-action breakdown); the
   "Write metadata for N changed photos" button never hides at 0 and shows
   on opening Sync.
2. Groups bulk-assign lives inside the Sync mode where nobody would look —
   it becomes its own place, and the fix-up 3 people picker moves with it.
3. George's full vent list — awaited; everything that makes him swear at the
   app, in any order, lands here.

## Web polish list (separate: "OK, but not great")
- Unsent suggestion text is dropped when another panel opens on the photo
  page — cost George his first member suggestion (2026-11-10). A draft
  should survive panel switches, or warn before discarding.
- George's web list — awaited.
