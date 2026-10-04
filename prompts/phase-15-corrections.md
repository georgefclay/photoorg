# Phase 15 — Corrections: fixing a mistake must never need SQL

Read `CLAUDE.md` (Inviolable; Web core / sync — especially fix-up 2's "content changes always re-sync" and "which database"; Faces People sidebar; Search triggers), `PROJECT-PLAN.md` (§2, §5), `shared/SCHEMA.md`. Work in `desktop/` and `web/`. Start with `git pull`.

WHY: a folder was named "Canaca" instead of "Canada". That typo became an album name and 139 pending description suggestions. Correcting it took SQL by hand, landed in the wrong database, needed a sync fix-up, and consumed an evening. George's verdict, which stands as a rule from now on: **people make mistakes; every piece of text a person can see must be correctable from the app that owns it, with an audit row, and the correction must reach every copy on its own.** Nothing in this archive is corrected with SQL again.

## Rule (add to CLAUDE.md Inviolable)
**Every human-visible text is editable in a UI.** Album names, suggestion text (pending), person names and variants, place names and aliases, back transcriptions, photo captions/notes, group names. Each edit writes an audit row with previous/new values and flows through sync without any further step. If a field has no edit path, that is a bug.

## Desktop: Corrections tool (new sidebar entry)
1. **Find & replace across text.** A search box; results grouped by where the text lives: albums, pending suggestions (description / folder / transcription), people (all name fields and variants), places (+ aliases), back transcriptions, photo notes. Each group shows count and sample rows; checkboxes per group and per row; a Replace field; **Preview** shows before/after per row; **Apply** writes with one audit row per changed row (`correction.replace`, carrying the search/replace and the ids) and a batch id so the whole correction can be undone (**Undo this correction** button, session-independent — it's in the audit).
2. **Albums editor**: list, rename, soft-delete, reorder photos, remove a photo from an album; audit rows. (The web stays read-only for albums until the pull-back exists — note it in the UI.)
3. **Direct edits** on the existing screens where they're missing: album name in the album view; suggestion text in the Suggestions block of the preview (edit-then-accept, or edit-and-leave-pending); place editor (name, aliases, lat/lon).
4. Anything edited here re-syncs on the next push — that is already true after Phase 9 fix-up 2; add a test per table that a Corrections edit reaches the web upsert.

## Web: admin corrections
1. **Suggestions queue**: an admin can **edit the text of a pending suggestion inline** before accepting it (audit `suggestion.edit`), and **bulk find & replace across pending suggestions** with the same preview/apply/undo shape as the desktop (admin only). These are web-born changes to desktop-pushed rows — reconcile: the desktop's next push must not overwrite a web edit with the old text. Mark the row `edited_on_web_at` and have the push skip payload for rows the web edited later than the desktop's `updated_at` (LWW by timestamp, same as `photo_groups`); the pull brings the web's text down to the laptop.
2. **People editor** for admins on the web (names, variants, suffix, years) — pulled back via the existing web-origin mechanism for web-born rows; for desktop-born people, LWW by `updated_at` on the name fields, both directions, with the pull applying web edits to the laptop. Tests for both directions and for the conflict case.
3. Album rename on the web stays deferred (open item 11) unless it falls out cheaply from the LWW work above — if so, do it and close item 11.

## Deploy
The VM is a migration behind (`phase-7-cleanup` not applied), which is also why pushes were failing silently. Deploy per the GC.md runbook as the **first** step of this phase, confirm `/sync/status` → `id_floor.ok` and that a push completes with no failed stages, and find out why the `photoorg` journal on the VM has had no entries since Sep 17 (logging is part of "mistakes are fixable").

## Tests
Find & replace preview/apply/undo per table; audit rows present with previous/new; web inline edit + LWW both ways; a desktop push after a web edit does not clobber it; people name LWW; migration state check in the deploy step.

## Verification, then stop
1. `pytest` and `npm test` green.
2. On real data: use the Corrections tool to change a harmless test string (e.g. add and then remove a trailing marker on one album name), push, confirm on the site, undo, push, confirm. Report the audit rows.
3. Deploy done; `/sync/status` healthy; one full push with zero failed stages pasted.
4. CLAUDE.md gets the rule; `PROJECT-PLAN.md` updated; commit and push: `Phase 15: corrections`.

---

## Questions from Claude Code (2026-10-04)

Findings first — two things in the prompt's premises turned out different from
what we believed, and both change the design:

**A. The `photo_masters` 500 is not the missing `region` column.** I had told you
it was. The VM's log (`/var/log/photoorg.log`) shows
`duplicate key value violates unique constraint "photo_masters_master_path_key"`
on `D:\Scanned Photos\Batch 00005\2025-10-29-14-23-0001.jpg` — a Phase 7 split
child. The VM still has the **old global unique** on `master_path`; Phase 7's
migration is what replaces it with a per-`region_key` one. Same root cause (the
migration is not applied), different mechanism.

**B. The journal was never silent — nothing was ever sent to it.** The unit has
`StandardOutput=append:/var/log/photoorg.log` (and the same for stderr), so
`journalctl -u photoorg` only ever shows systemd's own start/stop lines. Not a
bug; GC.md documents the file, I looked in the wrong place. The 500s were being
logged all along. If you want `journalctl` to work too, that's a unit change.

**C. `updated_at` cannot carry the LWW on the web side.** Every `/sync/*` upsert
sets `updated_at = now()`, so on the web `updated_at` means *"when a push last
touched this row"*, not *"when a human edited it"* — exactly the trap that forced
`tombstoned_at` to be its own column in Phase 7. So §Web/1's `edited_on_web_at`
is right, but §Web/2's "people: LWW by `updated_at` on the name fields, both
directions" would misbehave: after any push, the web's `updated_at` is newer than
the laptop's for every row, edited or not.
  (Related, and benign: the existing `photo_groups` LWW means an *unchanged* row
  is skipped on every push after the first. That is why my last push reported
  `photo_groups: 0` having sent 139 rows. Correct for LWW, just worth knowing.)

Now the questions. Each has my recommendation — "yes" to all of them is a
complete answer.

1. **"Photo captions/notes" — which column?** There is no `photos.caption` or
   `photos.notes`. The only text on `photos` is `physical_ref_note` (a physical
   locator — dedupe appends scan locators to it with `| `) and `description_ai`
   (an AI fact column; Inviolable says only an admin promoting a suggestion
   writes it). **Recommend:** the editable surface for a photo's words is the
   pending `description` **suggestion** (which Corrections already covers), and
   `physical_ref_note` becomes editable as itself. Leave `description_ai` alone.
   If you want a first-class human caption that is yours rather than the AI's,
   say so and I'll add `photos.caption` + a sync route — but I'd rather not
   invent a field you didn't ask for.

2. **One general `edited_on_web_at` pattern, or narrower?** Given finding C:
   **recommend** adding `edited_on_web_at timestamptz` to every table the web can
   edit (`suggestions`, `people`, `person_name_variants`, `places`, `albums`),
   with one shared rule: the push does not overwrite a field whose row has
   `edited_on_web_at > ` the laptop's `updated_at`, and `/sync/pull/confirmed`
   brings the web's text down. One pattern, one test shape, instead of five
   bespoke reconciliations. Alternative: suggestions only, and people stay
   desktop-authoritative (simpler, but then the web People editor is read-only
   for desktop-born people — i.e. for all 118 of them).

3. **Does find & replace touch the master-derived columns?**
   `photos.source_folder`, `photos.scan_batch` and `photo_masters.master_path`
   all still contain "Canaca" and must keep mirroring the read-only disk
   (`master_path` is also the key that makes re-ingest a no-op).
   **Recommend:** show them in the results as a **read-only group** —
   "mirrors the master disk, not editable" — with a count and no checkbox, so
   the typo's full extent is visible and the reason it stays is on screen.

4. **Undo when a row changed again after the correction.**
   **Recommend:** undo restores only rows whose current value still equals what
   the correction wrote; any row edited since is skipped and listed by id, so
   undo can never silently clobber newer work. (Undo stays available
   indefinitely since it reads the audit, per the prompt.)

5. **`album_photos` removal cannot sync today.** §Desktop/2 asks for "remove a
   photo from an album", but `album_photos` has no soft-delete and the push only
   upserts — so a desktop removal leaves the web's row in place forever. Same
   gap on `photo_places`. **Recommend:** a migration adding
   `is_deleted / deleted_at / deleted_by / updated_at` + `set_updated_at` to
   `album_photos` (and `photo_places` while we're there), so removals sync the
   way `photo_groups` does. Otherwise album removal is desktop-only and the site
   keeps showing the photo in the album.

6. **`place_aliases` has no sync route at all** (open item 12). If place names
   and aliases become editable, alias corrections stop at the laptop.
   **Recommend:** add `/sync/place_aliases` in this phase and close that half of
   item 12.

7. **The deploy needs your hand.** I took a fresh pre-deploy dump
   (`~/backups/photoorg-2026-10-04.dump.gz`, 08:12, 6.8 MB; disk 62 GB free),
   but the `git pull` on the VM was refused by the permission classifier as a
   production deploy. Either run the GC.md runbook yourself (four lines, pasted
   in my message) or tell me to proceed and approve the prompts. Everything in
   §Verification/3 waits on it; nothing else in the phase does.

---

## Answers to Claude Code's questions
(added as they come)
