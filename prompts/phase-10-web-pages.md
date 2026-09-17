# Phase 10 — Web pages (the site the family uses)

Read `CLAUDE.md` (Web core / groups section especially), `PROJECT-PLAN.md` (§2 Mobile-first, Groups, Contributor uploads, "Who is this?"; Phase 10), `web/README.md` (API summary from Phase 9), the existing `views/` and `public/css/site.css`, and `photo-archive-build-prompts.md` §10. Work in `web/`. Start with `git pull`. The site is live at `https://cyberdinosaurs.com` (deploy runbook in `GC.md`); deploy at the end of this phase.

Stack: **Express + EJS + plain JS + plain CSS**. No framework, no build step. The Phase 9 JSON API is complete; this phase is pages and the JavaScript that talks to it. Every page goes through the visibility rules already in the API — never query photos directly in a view.

AUDIENCE: relatives of mixed technical ability, most on phones. Design rules:
- One column on phones; grid widens on tablets/desktops. Tap targets ≥ 44 px. Text ≥ 16 px. No hover-only affordances.
- The site name is **Cyber Dinosaurs**. Header: name, group switcher, search box, avatar/menu. Footer: nothing important.
- Every action a contributor can take is at most two taps from the photo. Like is one tap.
- Pages must work with JavaScript disabled for reading; JS only enhances (infinite scroll, tagging box, autocomplete, upload progress).
- Loading states and empty states are designed, not blank.
- Keep `site.css` under ~600 lines; use CSS variables for colour/spacing; respect `prefers-color-scheme`.

## Layout
`views/layout.ejs`: header with group switcher (a `<select>` of "All my groups" + each group; stored in the session; every list page filters by it), search input (goes to `/search`, Phase 11 makes it smart — for now name + free text via the existing API), nav: Photos, People, Albums, Upload, (admin/moderator: Admin). Mobile: bottom tab bar with those five.

## Pages
- `/` **Browse** — grid of thumbnails (square crops), sort: Recently added (default) / Most liked / Least complete / Oldest / Newest by date. Infinite scroll via `/api/photos` keyset pagination; a "Load more" button as the no-JS fallback. Tapping opens the detail page. A small **Needs attention** strip at the top: "12 photos with no date", "8 untagged faces", "Who is this? (23)" — each a link to a filtered view.
- `/photos/:id` **Photo detail** — large image (fits width; pinch-zoom native), caption strip: date with a clear **confirmed / guess** marker (a guess shows the range and "help us date this"), people tagged (chips → person page), place, physical reference (`Batch 00012 #017`) for scans, groups it's in (admin/moderator only). Below: like button (prominent, shows count), comment thread (newest last, form at bottom, moderators see hide), the back image + transcription if present, and the actions row: **Tag a face**, **Suggest a date**, **Suggest a place**, **Add to album** (admin), **Rescan wanted** (admin). Prev/next within the current list (swipe on touch).
  - **Tag a face**: existing detected boxes are drawn (unassigned ones labelled "?"); tap a box → name autocomplete (`/api/people/autocomplete`) → submit creates a `person` suggestion. Draw a new box by drag (or two taps on touch) for a missed face. Tap a named box → "This isn't them" (dispute).
  - **Suggest a date**: one text field accepting `1962`, `March 1962`, `sometime in the 60s`, `summer 1971`; server parses (already implemented); show the interpretation before submit ("We'll record: 1962, precision year").
  - Pending suggestions on this photo are shown as "Someone suggested 1962" (name for admins/moderators).
- `/people` **People** — searchable list with face-count and year span; `/people/:id` person page: names (given/middle/surname/maiden/nickname/suffix), relationships (parents, spouse, siblings, children derived), a grid of every visible photo they're in, "Suggest a relationship" form. Contributors may create a person from the tag flow only.
- `/albums`, `/albums/:id` — virtual albums; admin can create/rename and add the current photo.
- `/who-is-this` **Who is this?** — feed of faces marked `unknown` (from the desktop) on visible photos: face crop + the whole photo + "I know who this is" → name autocomplete → `person` suggestion. Also reachable from the Needs-attention strip.
- `/upload` — replace the Phase 9 minimal page with the real one: big "Take a photo / Choose photos" buttons on phones, drag-drop + folder picker on desktop, group multi-select (only the user's groups; pre-selected if they have one), note field, per-file progress with retry, sha pre-check skip, "You've sent 14 photos, 12 awaiting approval, 2 approved" on `/upload/mine`.
- `/search` — name / free-text results using what the API supports today (Phase 11 upgrades it); filters: year range, person, place, album, "no date", "untagged faces".
- **Admin** (`/admin`): dashboard with counts and links → Suggestions queue (accept / reject / force on 409, grouped by kind, with the photo thumbnail and evidence), Disputes, Contributions (approve/reject per file and per batch, dup badges), Access requests, Users, Groups (create, members, moderators), Unfiled queue (bulk assign to group with the same filters as the API), Rescan list (printable, grouped by batch), Monthly report, Audit browser. Moderators see the subset scoped to their groups.

## JavaScript
Small modules under `public/js/`: `grid.js` (infinite scroll), `tagger.js` (boxes, draw, autocomplete), `upload.js` (rewrite of the Phase 9 script), `datefield.js` (live interpretation), `like.js`. No bundler; plain `<script defer>`. All POSTs carry the CSRF token from `/api/csrf` or the form field.

## Tests
- Supertest page smoke tests for every route: 200 for an authorised user, 302 to login for anonymous, 404 for a non-member on a photo, admin pages 403/404 for contributors.
- EJS renders with empty data (no photos, no groups) without errors.
- Date-field interpretation examples.

## Verification, then stop
1. `npm test` green.
2. Deploy per the GC.md runbook. On your phone (off Wi-Fi): browse, open a photo, like it, comment, tag a face, suggest a date, upload two photos into Clay Family; on the laptop as admin: approve the suggestions and the upload; on the desktop: Pull, confirm the contributed photos arrive. Report anything that needed more than two taps or felt slow.
3. Lighthouse (mobile) on `/` and a photo page: accessibility ≥ 90, no layout shift on image load (width/height attributes set).
4. Update `CLAUDE.md`, `web/README.md`; commit and push: `Phase 10: web pages`.

---

## Answers to Claude Code's questions

**A1. Id collision — good catch; fix it first as its own commit** (`Phase 9 fix-up 1: web-origin ids`) before any page work. Your plan is approved with these specifics:
- Put the floor in one shared constant (`shared/` — e.g. `WEB_ID_FLOOR`) read by the web, the desktop push, and the desktop pull. **Check the id column types first.** If those tables are `bigint`, use 1 000 000 000 000. If any are `serial`/`int4`, do NOT migrate types now — use 1 000 000 000 (fits int4; the desktop will never reach it) and say which tables are int4 in the report.
- Migration sets the VM sequences (`faces`, `people`, `suggestions`, `albums`, `album_photos` if it has its own id, `places`, `relationships`, `person_name_variants`, anything else the web can insert) to start at the floor. It is a no-op on the desktop DB — guard it so running the same migration file locally does not move the laptop's sequences (e.g. only when a `web_origin` setting/env is present, or make it idempotent and harmless: setting a sequence to a value below its current position must be skipped, and on the desktop the floor is *above* current — so gate it explicitly; document how).
- Belt and braces: the sync upsert refuses (400) any incoming id ≥ floor, **and** never overwrites an existing row whose id ≥ floor.
- Pull copies web-created `faces` (with `source='human'`, `embedding=null`, `embedding_stale=true` so the existing stale-embedding refresh computes it from the crop; generate the face crop at pull) and `people` down with their web ids. Pull also applies `photo.rescan_wanted` audit rows to `photos.rescan_wanted`.
- **Push runs Pull first**, always, so a web change can't be clobbered by a stale push. Note that in CLAUDE.md.
- Tests must seed both sides with overlapping ids and prove: web insert lands ≥ floor; desktop push of an id ≥ floor is refused; pull round-trips a web face and a web person; rescan_wanted set on the web survives a push.

**A2.** Name inside the suggestion. Nothing exists until accept; accept creates the person (web id ≥ floor) and assigns the face in one transaction, then pull brings it down.

**B3.** Yes — `services/` functions shared by the JSON API and page routes; visibility enforced there and only there.

**C4.** Yes to all seven. Keyset cursor must be `(sort_value, id)` composite for every sort. Keep `/api/search` deliberately small; Phase 11 replaces it.

**C5.** (b). Albums read-only on the web in Phase 10; no "Add to album" button. I've added bidirectional album sync to the plan's open items.

**D6.** Admins: "All photos", each group, "Unfiled". Contributors: "All my groups", each of theirs. The choice filters every photo grid and every count (browse, person page, album page, search, Who is this?, attention strip). People and Albums *lists* stay site-wide; their photo grids respect the filter.

**D7.** Correct. Also surface the moderator's existing "remove from my group" action on the photo page (groups strip) for moderators of that group.

**D8.** Any signed-in user. It's a suggestion; admin accepts.

**D9.** Yes. Keep it a short query string (`?from=<list-key>&cursor=…`), never a full filter dump; fall back to Browse order.

**D10.** Right split. Put the phone checklist at the end of your report and I'll hand it to George.

GO.

### PM notes on the mid-phase report (fix-up 1 live, foundation committed, agents running)

All good. Three things to confirm before the final commit — say so explicitly in the report:

1. **`/media/display` and the on-demand face crops go through the same visibility gate as `/media/*`** (404 for non-members, never served for private/deleted). Cache the derived files on disk under `PHOTO_DIR` so the second request is a file read, and make sure the cache key includes `file_version` so a re-pushed photo invalidates them.
2. **Face crops cut on the web must apply EXIF orientation first** (`sharp().rotate()` before `extract`), because `faces.bbox` is in the display frame (CLAUDE.md, fix-up 6). Cross-check one rotated phone photo against the desktop's crop.
3. **Backs push must be idempotent** like photo files — track a synced version for `photo_backs` so the second push sends zero back files; report the count on the VM after two pushes.

Also: the four agents' work goes through your own integration pass — run the full suites once on the merged tree, not just per-schema, and grep for any view that queries `photos` directly instead of going through `services/`.
