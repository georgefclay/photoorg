# Phase 11 — Search

Read `CLAUDE.md` (Web pages section: everything goes through `web/services/`, group scope, cursors, the fail2ban 4xx rule), `PROJECT-PLAN.md` (Phase 11), `shared/SCHEMA.md` (people, person_name_variants, the nickname seed table from Phase 1, places, suggestions payloads, photos.completeness), and `photo-archive-build-prompts.md` §9. Work in `web/` and `shared/migrations/`. Start with `git pull`. The site is live; deploy at the end per `GC.md`.

GOAL: search is how relatives find anything in 13,000 photos before dates and tags are complete. Replace the Phase 10 placeholder `/search` and `/api/search` with the real thing. Desktop code does not change in this phase (the migration runs on both DBs; keep it side-neutral).

## What a query matches
One search box. The query is split into tokens; each token is resolved to every layer it could mean, layers are OR-ed within a token and AND-ed across tokens, and results are ranked by the strongest layer that matched.

1. **People** — given, middle, surname, **maiden name**, suffix (`people.suffix`), and `person_name_variants`. A married surname and a maiden surname both find the same person.
2. **Nicknames** via the seed table (bidirectional: "Peggy" → Margaret, "Margaret" → Peggy/Peg/Meg). This is a lookup, not fuzzy matching — Peggy and Margaret share no letters.
3. **Misspellings** via phonetic matching (`fuzzystrmatch` `dmetaphone`, the genealogy standard) so Schmitt finds Schmidt and Katherine finds Kathryn — **guarded**: a phonetic-only hit also needs `pg_trgm` similarity ≥ 0.3 against the name it matched, and phonetic results rank below exact / nickname / variant hits. Never return nonsense.
4. **Full text** (`tsvector`, English config + `unaccent`) across comments (web-authoritative), AI descriptions (both the accepted `photos.description_ai` fact **and** the newest pending `description` suggestion — George will never hand-accept 12k descriptions, and search is discovery, not a fact display; rank the fact higher), back-of-print transcriptions, album names, and the folder-name hints. "the porch photo" finds it even if nobody tagged it.
5. **Dates**, tolerant of precision: `1962`, `1962-1965`, `1960s`, `60s`, `before 1950`, `after 1980`, `March 1962`. A photo whose date precision is `decade` appears in a search for any year inside that decade; a `year`-precision photo appears in its decade; a pending `date` suggestion with a range counts (ranked lower and labelled "estimated"). Reuse the existing date parser — one parser.
6. **Places** — name and any alias.

Result cards explain the match in plain words: "Margaret Clay (nickname of Peggy)", "Schmidt (sounds like Schmitt)", "back of print: 'Peggy and the boys, porch, 1962'", "date estimated 1958–1965".

## Filters and sorts
Filters (query string, no session state): person, place, album, year range, `no_date`, `untagged_faces`, `completeness_below=<n>`, plus the header group scope as everywhere. Sort: relevance (default), then the standard Browse sorts. Composite keyset cursors like every other list; relevance uses `(score, id)`.

## Implementation
- **Migration** (`shared/migrations/`): enable `pg_trgm`, `fuzzystrmatch`, `unaccent` (all in `postgresql-contrib`; check they're installed on the VM and the laptop before writing code — say so in your questions if not). Add `photo_search` (`photo_id` PK, `tsv tsvector`, `names text`, `updated_at`) and `person_search` (`person_id`, `token text`, `kind` = exact | variant | nickname | phonetic, `phonetic text`) with GIN indexes. Maintain both from **SQL triggers** on `photos`, `photo_backs`, `suggestions` (kind description/date), `comments`, `faces`, `people`, `person_name_variants`, `album_photos`, `photo_places` so the desktop and the VM stay correct without any Node or Python code knowing about it. Provide `select refresh_photo_search(photo_id)` and a one-time full rebuild function; the migration's `up` runs the rebuild. `down` drops everything it created and nothing else.
- **`services/search.js`**: `parseQuery(text)` → tokens with resolved layers (pure function, unit-tested heavily); `search(user, scope, parsed, filters, sort, cursor)` → results with `why` per hit. Visibility is applied here as in every other service — a non-member never sees a hit, a count, or an autocomplete suggestion for a photo they can't open.
- **Header search box**: autocomplete for people and places as you type (existing `autocomplete.js`), Enter runs the full search. Works without JS (plain GET form).
- **People page** search uses the same resolver, so "Peggy" finds Margaret there too.
- **Budget**: any query on the full archive under 150 ms server time; log slow queries over 300 ms with the parsed form.
- **Fail2ban rule** (CLAUDE.md): an empty search, a search with no hits, and an unknown filter value are all 200 pages, never 4xx.

## Tests
- Parser: every date form above; a token that is both a name and a word ("May", "June", "Rose"); quoted phrases; nonsense stays free text.
- Resolver: Peggy → Margaret; Margaret → Peggy; maiden vs married; Schmitt → Schmidt but not Smith; Katherine → Kathryn/Cathy/Kate ranked exact first; a phonetic near-miss with low trigram similarity is excluded.
- Dates: decade-precision photo appears in a year-in-decade search; year-precision photo appears in its decade; pending range suggestion labelled estimated.
- Full text: transcription hit, comment hit, pending-description hit ranked below accepted-description hit.
- Visibility: two users, one group each; a search never leaks the other's photo (results, counts, autocomplete).
- Triggers: updating a comment / accepting a description / assigning a face changes `photo_search` for that photo only.

## Verification, then stop
1. `npm test` green; migration up/down round-trips on `photoorg_test`.
2. Run the migration on the VM (rebuild time on 13k photos reported). Then run the spec's acceptance queries against the **real** archive and paste the top 5 of each with the `why` text: a nickname you find in `person_name_variants` / the seed that actually exists in `people`; a surname spelled wrong on purpose; a decade search; "porch" (or whatever word appears in real transcriptions — pick one from the data).
3. George on the phone: five searches of his choosing, including one by maiden name and one by nickname. Anything that returns nonsense or misses an obvious person is a fix-up.
4. Update `CLAUDE.md` (search section), `web/README.md`, `shared/SCHEMA.md`; commit and push: `Phase 11: search`.

---

## Answers to Claude Code's questions

1. **Plan A.** George runs the three laptop commands as `postgres` (below); you do the VM with `sudo -u postgres`. Keep `create extension if not exists unaccent` in the migration and make the failure message print the exact command. Record the prerequisite in `GC.md` (laptop + VM sections) and `web/README.md` ("fresh clone: a superuser must create `unaccent` first").
2. **Drop the Phase 1 machinery.** One search path. `down` recreates it exactly.
3. **Add `place_aliases` now** with the composite PK, no bigserial. Note in SCHEMA.md that it has no sync route yet (open item for Phase 12 alongside albums).
4. Your list is right. **Yes** to `source_filename` and `physical_ref_note`, both at the lowest weight (D) — named scan folders like "Grandma's 80th" are worth having; "IMG_1234" tokens are harmless at weight D. Put `description_ai` at A, people names at A, transcriptions at B, comments/album names/pending description at C, folder/filename/locator at D.
5. **Suggestions too**, ranked below the fact and labelled "suggested". Same rule as descriptions.
6. **Measure before adding the GUC.** Write the triggers as statement-level with transition tables where the table allows it (faces, suggestions, comments — the bulk writers), which removes most of the per-row cost on its own. Then on `photoorg_test` insert 5,000 faces rows + 5,000 suggestions rows in one transaction with and without the triggers and report both times. If the trigger overhead is under ~2 s for that batch, skip the GUC. If it's more, add the deferred dirty-table + sweep exactly as you describe, off by default. Either way the number goes in the report.
7. **Yes**, spec names as aliases; `completeness_below=<n>` wins over `low_completeness`. Document the one vocabulary in `web/README.md`.
8. **Keep the strip + grid.** The people/places strip appears only when a token resolved to a person or place; the grid always carries the `why` line.
9. **Agreed — Phase 12.**
10. **Yes, do it yourself** over SSH. Accepted-above-pending on `photoorg_test` fixtures is fine. Two extras while you're on the VM: after the migration, confirm `/sync/status` → `id_floor.ok` is still true (the migration touches sequences' neighbours, not sequences, but check), and tail the caddy access log during your smoke searches for any 4xx.

GO.

George runs on the laptop (once):
```
psql -U postgres -d photoorg      -c "create extension if not exists unaccent"
psql -U postgres -d photoorg_web  -c "create extension if not exists unaccent"
psql -U postgres -d photoorg_test -c "create extension if not exists unaccent"
```
