# Phase 4 — Dedupe

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§1–§2, Phase 4), `shared/SCHEMA.md`, `photo-archive-build-prompts.md` §4, and the Triage decision code (`modes/triage/decisions.py`) which you will reuse for quarantining. Work in `desktop/`. Triage is complete; dedupe operates on `triage_status = 'keep'` photos only (private photos are included — they can have duplicates too; junk is excluded).

GOAL: find photos that are the same picture — scanned twice, a phone file and a scan of its print, a resized export next to its original — and let George pick the keeper quickly. The loser is quarantined (soft-deleted, restorable), never deleted. Physical references and metadata are never lost.

## Candidate detection (job `dedupe_scan`, re-runnable, incremental)
- Compare pHash and dHash (16×16, 256-bit) across all keep photos. Two photos are candidates if pHash Hamming ≤ `DEDUPE_PHASH_MAX` (default 10) **or** dHash ≤ `DEDUPE_DHASH_MAX` (default 10). Both thresholds are Settings values.
- Scale: ~13k photos → do not do 13k² Python loops. Use BK-tree or multi-index hashing (split the 256-bit hash into 8 32-bit bands; candidates share at least one exact band, then verify full distance). Must complete in minutes, not hours.
- Also consider mirrored and 90°/180°-rotated variants: compute hashes of the flipped/rotated thumbnail at scan time and match against those too (scans get put on the glass any way up). Store which transform matched.
- Group with union-find so 3+ copies become one group. Store groups in `dedupe_groups` (id, status pending/resolved/not_duplicates, created_at, resolved_at) and `dedupe_members` (group_id, photo_id, distance_to_keeper, transform, is_keeper). New migration.
- Pairs George has marked "not duplicates" live in `dedupe_exclusions` (photo_a, photo_b, created_at) and are never re-proposed, even after re-scan.
- Report: number of groups by size, and a histogram of min distances.

## Keeper pre-selection
Score each member; highest wins, ties broken by lower id:
1. Has real EXIF `DateTimeOriginal` and camera model (digital original) — strongest.
2. TIFF over JPG.
3. More pixels.
4. Larger file.
5. Scan over digital when neither has EXIF (the scan carries a physical reference).
Show the reason chain in the UI ("keeper: has EXIF > 2× pixels").

## Review UI (Dedupe mode)
- Queue of pending groups, sorted by group size desc then distance asc. Header "group n / N, size k".
- Side-by-side at full resolution for pairs, with synchronised zoom (wheel) and pan (drag); for groups of 3+, a filmstrip of members with the keeper highlighted and any member swappable into the comparison pane by clicking or number keys.
- Per member: batch/sequence or folder, filename, dimensions, file size, mime, EXIF date if any, is_scan, triage status, transform matched, distance.
- Keys: `A` accept the pre-selected keeper (quarantine the rest), `1–9` pick a different keeper then `A`, `N` not duplicates (adds exclusions for every pair in the group), `S` skip (stays pending), arrows move, `Z` undo (restores quarantined members, reopens the group).
- Progress persists in the DB; closing the app and returning resumes at the first pending group.

## What "quarantine the rest" does
Reuse `apply_decision` from triage with a new reason `dedupe_loser_of <keeper_id>` in the audit row. Additionally:
- If a loser is a scan and the keeper is not, copy `scan_batch`/`scan_sequence`/`source_filename` onto the keeper's `physical_ref_note` ("also scanned: Batch 00012 #017").
- If a loser has a `photo_backs` row, re-point the back to the keeper (`photo_backs.photo_id = keeper`) and note it in the audit row.
- If a loser has accepted rescan masters (`photo_masters` rows), move those rows to the keeper as non-preferred masters.
- If a loser is in albums or has suggestions, move album membership and suggestions to the keeper (do not duplicate).
- `is_private`: if any member is private, the keeper becomes private.
Undo reverses all of it (store what was moved in the audit row's `new_value`).

## Tests
- Multi-index candidate search finds every pair with distance ≤ threshold on a synthetic set of 2,000 hashes (compare against brute force).
- Rotated/mirrored variants match.
- Union-find grouping; exclusions suppress re-proposal.
- Keeper scoring on the five rules.
- Resolve then undo: files, `photo_backs`, `physical_ref_note`, albums, suggestions all round-trip.

## Verification, then stop
1. `pytest` green.
2. Run `dedupe_scan` over the keep set; paste group counts by size, the distance histogram, and elapsed time.
3. George resolves 30 groups; report how many keeper pre-selections he overrode and whether any "not duplicates" were obviously wrong candidates (calibrates the thresholds).
4. Update `CLAUDE.md` and `shared/SCHEMA.md`.
5. Commit: `Phase 4: dedupe`.

---

## Answers to Claude Code's questions

1. In-memory index, rebuilt per run. No persistence.
2. (a). Compute variant hashes from the thumbnail on the fly.
3. Union of the two candidate sets. Store both distances on `dedupe_members` (`phash_dist`, `dhash_dist`, nullable) and `matched_by` (`phash` | `dhash` | `both`). `distance_to_keeper` = min of the two.
4. All seven: identity, mirror, rot90, rot180, rot270, rot90+mirror, rot270+mirror.
5. Treat equally; review discriminates. But show a "burst" badge on members that share a burst group so George sees why.
6. `photos.exif_taken_at` and `photos.exif_camera` exist (migration 2). Use them; no re-read.
7. `photos.is_scan` **exists** (migration 2, `boolean not null default false`) and ingest sets it. Use the column.
8. Confirmed: lower id last, deterministic.
9. Confirmed as proposed: drop and rebuild pending groups; leave resolved/not_duplicates alone; honour exclusions.
10. Confirmed: all pairs.
11. Skip = stays pending, moves to the tail of this session's queue, reappears on the next run.
12. Stack across the session, single-group at a time (Z pops the most recent). Not across restart — after restart, the quarantine browser is the way back.
13. QGraphicsView per pane with a shared transform is fine; there is no existing viewer helper. Put it in `app/widgets/synced_viewer.py` so Phase 7 (Cleanup before/after) can reuse it.
14. Append with ` | `. Single line.
15. Keep both back rows on the keeper. Backs are evidence; two backs is fine. Show "2 backs" in the UI after resolve.
16. Confirmed: keeper's preferred master and sha256 unchanged; loser masters re-parented as non-preferred.
17. Confirmed: skip duplicates, never error.
18. Confirmed: losers are quarantined regardless; only the keeper's flag changes.
19. `desktop/.env`: `DEDUPE_PHASH_MAX=10`, `DEDUPE_DHASH_MAX=10`, surfaced in the Settings dialog like the other values.
20. Confirmed: `(least, greatest)` with a unique index.
21. One migration via `npm run migrate:create -- dedupe-groups` (timestamp naming, as all the others).
22. `conftest.py` provides `test_database_url()` and the availability check; it does **not** migrate or truncate. Add a session-scoped fixture that runs `migrate up` on the test DB once, and a function-scoped fixture that truncates the tables a test touches. Keep it in `conftest.py` so Phases 6+ inherit it.
23. Yes. `tools/dedupe_report.py --dry-run` prints group counts at the configured thresholds plus the 50 nearest pairs above threshold (11–20) with thumbnails to an HTML file under `%LOCALAPPDATA%\PhotoArchive\reports\`. Run it before George reviews; paste the counts.

GO.
