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

8. **Comment bodies have no edit path at all** (found 2026-10-04). `comments`
   is insert-only in `routes/api-contrib.js`; the only correction a moderator
   has is hide/unhide. Under the new rule a comment is human-visible text a
   person can see, so this is a day-one exception unless we close it.
   **Recommend:** the author may edit their own comment and an admin may edit
   any, both writing `comment.edit` with previous/new. It stays web-only —
   bodies are web-authoritative and `GET /sync/pull/confirmed` deliberately
   sends summaries, not bodies, so nothing changes on the laptop.
   (Group names, the other item in the rule's list I had not checked, are
   already done: `PATCH /api/admin/groups/:id` writes a `group.update` audit
   row with previous/new. No work needed there. Back transcriptions already
   re-sync — `/sync/photo_backs` assigns `transcribed_text` in its
   `do update set`.)

9. **Does find & replace run on the web's own text too, or only on the rows
   the laptop owns?** The desktop tool as specced reaches albums, suggestions,
   people, places, backs and notes — all desktop-owned. Comments, group names
   and group descriptions exist only on the web.
   **Recommend:** the desktop tool stays on desktop-owned text and the web's
   admin find & replace stays on pending suggestions (as §Web/1 says), with
   comments and group text corrected one at a time in their own editors. A
   cross-tier find & replace over comment bodies would mean the desktop
   reading web-authoritative text it must never push back, which is how the
   `photo_groups` and `person_name_variants` bugs started.

---

## Answers to Claude Code's questions

Findings A–C accepted; C changes the design and your fix for it is right. Yes to all seven, with the following specifics:

1. **Yes.** No invented caption field. The editable surface is the pending `description` suggestion; `physical_ref_note` editable as itself. Leave `description_ai` alone.
2. **The general pattern.** `edited_on_web_at` on `suggestions`, `people`, `person_name_variants`, `places`, `albums`; one rule, one test shape. Also add `edited_on_desktop_at` (set only by human edits on the desktop — Corrections, People editor, album editor — never by jobs or sync) so the comparison is human-edit vs human-edit, not human-edit vs "when a push touched it". Ties: web wins (it's the one a relative sees). Document the two columns in CLAUDE.md next to `tombstoned_at` as the third member of that family.
3. **Yes** — read-only group, count shown, reason on screen.
4. **Yes** — skip-and-list, undo reads the audit.
5. **Yes** — migration for `album_photos` and `photo_places` soft-delete + `updated_at`, synced like `photo_groups`. Run it on the VM in the deploy step.
6. **Yes** — `/sync/place_aliases` now; close that half of item 12.
7. **Proceed with the deploy yourself; George approves the prompts.** Order: dump already taken → `git pull` → migrations (Phase 7 cleanup, this phase's) → restart → `/sync/status` → one full push with zero failed stages. Note in GC.md that `journalctl` is empty by design and `/var/log/photoorg.log` is the log.

Questions 8 and 9 arrived after the seven above were answered. Answered 2026-10-08; GO still stands.

8. **Yes** — the author may edit their own comment, an admin may edit any; both write `comment.edit` with previous/new. Web-only: bodies stay web-authoritative and `GET /sync/pull/confirmed` keeps sending summaries, so nothing changes on the laptop or in Phase 13's writer. Two specifics: (a) **moderators keep hide/unhide and do not get edit** — hiding is moderation, rewriting another person's words is an admin act; (b) **an edited comment says so** — add `comments.edited_at` (null until the first edit) and render "(edited)" next to the timestamp, so text never silently changes under a relative who already read it. Same visibility gate, CSRF carrier and per-user rate limit as every other contributor write; a non-visible photo is 404, never 403.

9. **Yes** — each tier's find & replace stays on the text that tier owns. Desktop: albums, pending suggestions, people (all name fields + variants), places (+ aliases), back transcriptions, `physical_ref_note`. Web (admin): pending suggestions only. Comments and group name/description are corrected one at a time in their own editors. The desktop never reads web-authoritative text it must not push back — that is exactly how the `photo_groups` and `person_name_variants` faults began. One specific: the web bulk apply stamps `edited_on_web_at` on every row it changes (so answer 2's rule shields it from the next push) and writes one audit row per changed row plus the batch id, the same shape as the desktop, and its undo is answer 4's skip-and-list.

Housekeeping: the "open item 12" cited in question 6 / answer 6 is PROJECT-PLAN §5 **item 11** (item 12 is the Phase 10 phone checklist, already done). The `place_aliases` half of item 11 is marked as Phase 15 in the plan; close it there in your step 4.

GO.

---

## Progress

### Built 2026-10-08

**One migration**, `phase-15-corrections`:
- `edited_on_desktop_at` / `edited_on_web_at` on `suggestions`, `people`,
  `person_name_variants`, `places`, `albums`, plus
  `sync_web_edit_wins(web, desktop)` — the LWW rule as one SQL function so the
  five upserts that apply it cannot each spell it differently. Ties to the web.
- Soft-delete (`is_deleted / deleted_at / deleted_by / updated_at` +
  `set_updated_at`) on `album_photos` and `photo_places`, with
  `refresh_photos_search_now` re-created so a removed album or place leaves the
  search vector, and live partial indexes on both.
- `person_name_variants.updated_at` (it had none) and `comments.edited_at`.
- `down` round-trips. It **deletes** soft-deleted join rows rather than dropping
  the column under them: without the flag the row reads as live again, which
  would be a silent re-add of a photo somebody removed from an album.

**Desktop** — `modes/corrections/`, registered between People and Sync:
- `targets.py` declares every place human-visible text lives, one `Target` per
  (table, field), pure of Qt and the database. Three row shapes: a plain
  column, a jsonb path (`jsonb_set`, so the rest of a suggestion's payload
  survives), and a composite key (`place_aliases`, where the text *is* the key).
- `repo.py` — search, preview, apply under one `batch_id` with a
  `correction.replace` row per change, and undo read back from the audit log.
  `replace_text` is the single place a replacement is computed, so the preview
  is literally the string that gets stored.
- `albums.py` / `places.py` and a three-tab `ui.py`.
- `faces/repo.py`'s person update and variant add now stamp
  `edited_on_desktop_at` as well.

**Sync** — push carries the human-edit stamp on all five tables and the
soft-delete flags on both join tables; a new `place_aliases` stage sends every
desktop place's whole alias set, alias-less places included (the empty set is
how "the last alias was removed" travels). `pull_web_edits` brings the web's
wording down before the push, paging on a composite `(edited_on_web_at, id)`
cursor.

**Web** — `services/corrections.js` and `/admin/corrections` (admin-only bulk
find & replace, same preview/apply/undo shape); inline text edit on the
suggestions queue; admin People editor (`PATCH /api/people/:id`, variant add /
rename); comment edit for the author or an admin with an "(edited)" marker;
`/sync/place_aliases`; `/sync/pull/web_edits`; the Phase 15 guards inside the
five `/sync/*` upserts; and `is_deleted = false` added to every reader of
`album_photos` / `photo_places` across `services/`.

### Two bugs the tests found, both worth keeping in mind

1. **An alias's text is part of its primary key**, so correcting it renames the
   row — and the key recorded in the audit entry is stale by the time undo
   reads it. Undo silently skipped every alias it should have restored until
   `repo.key_after_write` existed. It is the only shape where the key moves, so
   it has its own test.
2. **`_apply` and `_undo` both re-run the search afterwards**, so a single
   status label wiped the result of the action a moment after showing it. The
   status line is now the Phase 7 two halves — decision on the left, which
   persists; context on the right, which the search refreshes.

### And one test that had to be taught to see through an interpolation

`web/test/sync-resync.test.js`'s structural sweep reads `routes/sync.js`'s
*source* to prove every pushed column is also assigned on conflict. The new
guards reach the SQL through a `keepWebEdits()` call, so the sweep now expands
it with the route's own exported helper and asserts nothing is left
unexpanded. A sweep that cannot see through an interpolation is a blind spot,
not a pass.

### Tests

Desktop +45: `test_corrections.py` (21 — all three shapes previewed, applied
and audited; the read-only master group refused even when a caller ticks it;
undo restoring, and skipping a row edited since), `test_corrections_sync.py`
(15 — LWW both ways and the tie, web-origin rows left to `pull_web_origin`,
paging past a shared timestamp, the new push selectors), `test_corrections_ui.py`
(9 — the master group has no checkbox at all, group ticks cascade, apply/undo
round-trips through the real panel, the decision half survives the search).

Web +35 (`test/corrections.test.js`): literal-replacement semantics matching
the desktop's, inline edit leaving the rest of the payload alone, resolved
suggestions untouchable, bulk preview/apply/undo with skip-and-list, a push not
clobbering a web edit *and* a later desktop edit winning, a desktop merge still
re-parenting a variant the web renamed, the composite cursor, place-alias round
trips including the empty set, join-table removals reaching the site and
leaving search, and the comment-edit permission matrix (author yes, admin yes,
moderator no, non-member 404).

### Still open

- **The VM deploy (step 1) has not run.** The auto-mode permission classifier
  refuses remote writes (`[Remote Shell Writes]`) before George ever sees a
  prompt, so answer 7's "proceed and I'll approve" cannot be honoured from
  here. The runbook is in `GC.md`; §Verification/2 and /3 wait on it.
- Album create / rename / re-order on the web stays deferred to Phase 12
  (PROJECT-PLAN §5 item 11). Phase 15 carries a web *name* edit down to the
  laptop, but nothing lets the web make an album, so the page stays read-only
  and the desktop's Albums tab says so on screen.
- `person_name_variants` has no soft-delete, so a variant *removed* on the
  desktop still does not reach the web (a renamed one now does). Same class of
  gap as `album_photos` before this phase; out of scope here because answer 5
  named the two join tables only.

---

## Phase 15 fix-up 1 — the last real deletes, the `down` precedent, and the deploy

PM review of 2b59e6d (2026-10-08). The build is accepted as far as it goes: tests green; the desktop round trip on real data is clean (audit #78009–#78012, 139 suggestions untouched by choice); the guard reads correctly in `routes/sync.js` (`edited_on_web_at` is never in a push's insert list, so the web's stamp cannot regress; the `albums` upsert is still floor-guarded); the migration is additive and safe for the VM; and teaching the structural sweep to see through `keepWebEdits()` was the right instinct. Not pushing against the laptop web was the right call too. Three things before the phase closes, then the deploy and the two verification items that wait on it.

### 1. `person_name_variants` gets soft-delete — the gap you flagged is the rule's gap
A nickname removed on the desktop that keeps showing on the web is exactly the fault the Inviolable names, so it does not wait for another phase. Migration `phase-15-fixup-1`: `is_deleted / deleted_at / deleted_by` on `person_name_variants` (it has `updated_at` since Phase 15). Desktop "remove variant" (People sidebar and Corrections) becomes a soft-delete with a `person.variant_remove` audit row carrying the previous value; `/sync/person_name_variants` pushes and assigns the flag; the web People editor's variant removal does the same and stamps `edited_on_web_at`; add `is_deleted` to `WEB_EDITABLE.person_name_variants` so the same `sync_web_edit_wins` rule decides a removal exactly as it decides a rename. Every reader filters `is_deleted = false`, `person_search` included (re-create the Phase 11 function the way Phase 15 did for `photo_search`). A merge moves live variants only. Re-adding a removed variant flips the flag back — the key still exists — and that gets a test.

### 2. `place_aliases` — same treatment, not an exception
`/sync/place_aliases` does a real `delete`, and I assume the desktop's alias removal does too. "No real deletes, ever" has no exception for text that is its own key. Add `is_deleted / deleted_at / deleted_by / updated_at` (+ `set_updated_at`) to `place_aliases`; the desktop removal soft-deletes with a `place.alias_remove` audit row carrying the previous value; the sync route keeps its replace-the-whole-set semantics but writes them as `update … set is_deleted = true where place_id = $1 and alias <> all($2)` plus an upsert that sets `is_deleted = false` for each incoming alias; readers (the search place layer, autocomplete, the place editor) filter the flag. The empty incoming set still means "no live aliases". A Corrections rename of an alias keeps renaming the row in place (the audit has previous/new; `key_after_write` stays) — on the web that arrives as old-flagged, new-live. If you see a reason this is wrong rather than merely more work, say so under Answers and stop.

### 3. Phase 15's `down` aborts, it does not delete
`delete from ${t} where is_deleted` in `down` is a real delete. The precedent is `photo-back-orphan`'s `down`: abort with a clear message naming the counts when any soft-deleted rows exist, and say what the operator should do instead. Amend `down` in place; `up` stays byte-for-byte identical, because the laptop's `photoorg`, `photoorg_web` and the test DBs have already applied it and the VM must apply the same thing. Fix-up 1's own migration follows the same rule.

### 4. Two checks, no code unless they fail
- Re-adding a photo to an album it was removed from (and to a place) flips `is_deleted` back rather than failing on the key — on the desktop and through `/sync/album_photos` / `/sync/photo_places`.
- The soft-delete UPDATE on `album_photos` / `photo_places` fires the Phase 11 search refresh on the **laptop** too (the web test proves the web side only). If the Phase 11 trigger on either table covers only insert/delete, add update to it in the fix-up migration.

### 5. Deploy — George's hand, one window
The auto-mode classifier cannot be argued with, so George runs it. Commit and push fix-up 1 **first**, so the VM takes `phase-7-cleanup`, `phase-15-corrections` and `phase-15-fixup-1` in one window. Give George the one-liner again with the runbook's dump line prepended (it is in `GC.md`; the 2026-10-04 dump is five days old) — in your message, never in this file. When it returns `ok`, verify read-only from the laptop: `select name, run_on from pgmigrations order by run_on desc limit 4` shows the three; `/sync/status` → `id_floor.ok` and the VM's own identity; `/var/log/photoorg.log` clean since the restart; the caddy access log shows no 4xx burst from the new pages (the fail2ban rule).

### 6. Then finish Phase 15's verification on the site
- One full push with **zero failed stages**, pasted. This is the first push past `photo_masters` since Sep 17 — masters metadata, split children and tombstones should all flow; report the counts per stage.
- The prompt's step 2, second half: album 6 `(TEST)` → push → shown on the site → undo → push → shown restored.
- One variant removal → push → gone from the person page and from search → re-add → push → back. Report the audit rows for both.

### 7. Docs, commit
`CLAUDE.md` Inviolable: "No real deletes" now names `person_name_variants` and `place_aliases` as soft-delete tables, and states the migration-`down` rule (abort, never delete). `shared/SCHEMA.md` for the new columns. `PROJECT-PLAN.md` Phase 15 section, §5 item 17 (deploy done) and a Progress entry. Commit and push: `Phase 15 fix-up 1: soft-delete variants and aliases, down aborts, deploy`.

### Not in this fix-up
`sync_state.json` pull cursors are not keyed by the target web's identity — pointing the desktop at a second web server would cross them (your reason for not testing against the laptop web, correctly). Recorded as PROJECT-PLAN §5 item 18; fix when a second target ever exists.

### Fix-up 1 built 2026-10-08

No objection to any of the three — all accepted and built. Item 2 in
particular: "no real deletes, ever" having no exception for text that is
its own key is right, and the row keeping its `(place_id, alias)` key
while carrying the flag costs nothing.

**Migration `phase-15-fixup-1`**
- `person_name_variants`: `is_deleted / deleted_at / deleted_by` + a live
  partial index (it already had `updated_at` from Phase 15).
- `place_aliases`: `is_deleted / deleted_at / deleted_by / updated_at` +
  `set_updated_at` + a live partial index.
- Phase 11's `refresh_people_search_now` re-created with
  `nv.is_deleted = false` on the `variant_tokens` CTE — the same move
  Phase 15 made for `photo_search`. Its `person_name_variants` update
  trigger already unions `ot`/`nt`, so an `is_deleted` flip rebuilds the
  index with nothing else needed.
- `down` aborts on soft-deleted rows, as does Phase 15's amended one.

**Desktop**
- `faces/repo.remove_name_variant` soft-deletes and audits
  `person.variant_remove` with the previous value; `add_name_variant`
  flips a removed row back (matching on `lower(variant)`, which is what
  the unique is on) and returns the *same id*, so the web's copy stays in
  step; `list_name_variants` and the people autocomplete filter the flag.
- `merge.py` moves **live** variants only and soft-deletes the colliding
  leftovers instead of deleting them — those rows are pushed, so a real
  delete left the web showing a nickname on a person who no longer exists.
- `corrections/places.remove_alias` soft-deletes and audits
  `place.alias_remove`; `add_alias` restores; `list_aliases` filters.
- Corrections' `person_variant` and `place_alias` targets no longer offer
  removed rows. The alias **collision check deliberately stays
  unfiltered**: a soft-deleted row still holds its key, so renaming an
  alias onto a removed one is still refused-and-listed rather than
  aborting the batch.
- Push sends the variant flag and `deleted_at`; the `place_aliases` stage
  sends each place's **live** set.

**Web**
- `WEB_EDITABLE.person_name_variants` gains `is_deleted` (and `deleted_at`,
  so the timestamp travels with the flag it belongs to), so
  `sync_web_edit_wins` settles a removal exactly as it settles a rename.
  `people.is_deleted` is deliberately *not* in any of these lists.
- `DELETE /api/people/:id/variants/:variantId` — admin only, soft-delete,
  stamps `edited_on_web_at`, audits `person.variant_remove`. The People
  editor grows a **Remove** button. `POST .../variants` now restores a
  removed variant (same id) instead of 409-ing on the key.
- `/sync/place_aliases` flags what the laptop no longer sends and un-flags
  what it does. Its per-alias write is **update-then-insert, not one
  upsert**: the unique is on `lower(alias)`, so a desktop rename that only
  changes case leaves a row whose `alias` no longer matches but whose
  `lower(alias)` still does, and `on conflict (place_id, alias)` would miss
  it and then violate `place_aliases_ci_uq`.
- Readers filtered: `services/people.js` (both variant `exists` clauses and
  `getPerson`), `services/search.js` (both alias lookups).

**Item 4 — both checks passed with no code change, and now have tests.**
Re-adding a photo to an album or place already flips the flag rather than
failing on the key, on the desktop and through both sync routes. Phase
11's statement triggers on `album_photos` and `photo_places` already cover
UPDATE (`photo_search_touch_photo_id` unions `ot` and `nt`), so the
laptop's own index refreshes on a soft-delete — `test_corrections_fixup1.py`
asserts that directly against `photo_search`.

**One bug caught while writing it.** The route's `restored` counter read
`returning is_deleted`, which hands back the value just written (always
false) — so it reported every pre-existing alias as restored. It now comes
from a CTE that captures the flag before the update.

**Tests:** desktop +14 (`test_corrections_fixup1.py`), web +10
(`test/corrections-fixup1.test.js`). Totals: desktop 646 + 1 skipped, web
207.

**Not in this fix-up, recorded as PROJECT-PLAN §5 item 18:**
`sync_state.json`'s pull cursors are not keyed by the target web's
identity, so pointing the desktop at a second web server would cross them.
Harmless with one target.

**Still waiting on George:** the deploy (§5 and §6). Fix-up 1 is committed
and pushed, so the VM takes `phase-7-cleanup`, `phase-15-corrections` and
`phase-15-fixup-1` in one window.

---

## Answers to Claude Code's fix-up 1 questions
(added as they come)

None — nothing in fix-up 1 looked wrong rather than merely more work, so it was built as specified. The two judgement calls are noted in the Progress entry above: `deleted_at` rides along with `is_deleted` in `WEB_EDITABLE` (it is part of the same one fact), and the alias collision check stays unfiltered on purpose.
