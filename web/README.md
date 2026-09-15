# web

Node 24 + Express + EJS + Postgres. Private family archive site.
Deploys to an AWS VM (git pull + systemd + Caddy) same as George's other sites.
Serves images from `PHOTO_DIR`; auth is magic-link only, no open signup.

## Setup

```
npm install
copy .env.example .env
# edit .env — set SESSION_SECRET, SERVICE_TOKEN, Postmark keys
npm run dev
```

Listens on `PORT` (default 8090).

## Env vars

| Name | Notes |
| ---- | ----- |
| `PORT` | HTTP listen port. Default 8090. |
| `NODE_ENV` | `production` in prod so the session cookie gets `secure`. |
| `BASE_URL` | Absolute URL used inside emailed links, e.g. `https://photos.example.com`. Falls back to `http(s)://<host>` from the request if unset. |
| `DATABASE_URL` | Local Postgres. URL-encode special chars in the password. |
| `TEST_DATABASE_URL` | Separate DB used by `npm test`. Must not equal `DATABASE_URL`. Shares `photoorg_test` with `shared/`. |
| `SESSION_SECRET` | Random bytes. Rotate to log everyone out. |
| `ADMIN_EMAIL` | Single address that receives access-request notifications. Other admins are not copied. |
| `SERVICE_TOKEN` | Bearer token the desktop app presents on sync endpoints. Must equal `WEB_API_TOKEN` in `desktop/.env`. Constant-time compared. |
| `POSTMARK_API_KEY` | Leave blank in dev to use the dev-mail fallback. |
| `POSTMARK_FROM_EMAIL` | Verified sender in Postmark. |
| `PHOTO_DIR` | Root of photo storage on this host. Subfolders `working/`, `thumbs/`, `backs/`, `faces/`, `uploads/` are created on demand. On the laptop keep this distinct from the desktop's `WORKING_DIR` so a Sync push doesn't copy files onto themselves. |

## Bootstrap the first admin

```
node tools/create-admin.js georgefclay@example.com --name "George Clay"
```

Refuses if the email already exists. Then start the server and sign in via
`/login` — no admin can be created any other way.

## Auth flow

- **Request access.** `GET /request-access` → form (email, optional display
  name, optional message, honeypot). `POST` creates an `access_requests`
  row with a 72 h token and emails `ADMIN_EMAIL` with confirm-page links
  for approve and deny. Duplicate pending requests are silent.
- **Admin decision.** `GET /admin/access/:token/{approve,deny}` renders a
  confirmation page. `POST` performs the action. Approve creates the user
  (role `contributor`, status `active`) if absent and emails them their
  first magic link. `GET /admin/access` (session-authed) lists pending
  requests with buttons for the same actions.
- **Magic-link login.** `GET /login` → form. `POST` (rate-limited) creates
  a `magic_links` row with a sha256-hashed token and emails the raw link
  to the user; response is always the same "check your inbox" page
  regardless of whether the email is known. `GET /a/:token` renders a
  confirmation page. `POST /a/:token` marks the link used, regenerates
  the session, and signs the user in.
- **Sessions.** Cookies are 180-day, `httpOnly`, `sameSite: lax`, `secure`
  in production. Storage is Postgres via `connect-pg-simple`. On
  every request, `loadUser` looks the user up and treats the session as
  anonymous (and destroys it) if the user is not `active`. Suspend also
  purges the user's `session` rows for tidiness.
- **CSRF.** Per-session token exposed as `res.locals.csrfToken`. Every
  authed POST form must send it as `_csrf`. Pre-auth POSTs (`request-access`,
  `login`) rely on the honeypot + rate limit; token-URL POSTs (`/a/:token`,
  `/admin/access/:token/{approve,deny}`) rely on the unguessable token.
- **Rate limit.** 5 attempts / 15 min per IP on both `/request-access`
  and `/login`, each with its own counter.
- **Service account.** `Authorization: Bearer <SERVICE_TOKEN>` with a
  constant-time compare. No `users` row, no session, never appears in the
  admin list. Exposed by `GET /service/ping` as a smoke endpoint; real
  sync routes ship in Phase 9.

## Dev mail

If `POSTMARK_API_KEY` is unset, `services/email.js` logs every message to
the console and writes a copy to `web/tmp/mail/<timestamp>.txt` so the
sign-in links are clickable in dev. The `tmp/` directory is gitignored.

## Phase 9 API surface

Session-authed JSON under `/api`. Every list is keyset-paginated (by
id desc, `cursor` param) and visibility-scoped (see below). Session
POSTs carry `_csrf` from a form field or `X-CSRF-Token` header;
`GET /api/csrf` returns the current token. Rate limit: 300/hour per
user combined across contributor writes; admins exempt. Contributor
uploads: 600/hour per user.

- `GET  /api/photos` — filters `year|decade|person_id|place_id|album_id|has_no_date|has_untagged_faces|low_completeness`, sort `recent|liked|incomplete`.
- `GET  /api/photos/:id` — faces (with disputed flag), comments, place, likes, backs (transcription), pending suggestions, physical ref, rescan_wanted. Suggester name shown only to admins + moderators of a group the photo is in.
- `POST /api/photos/:id/{suggestions,faces,comments,like}` and `POST /api/faces/:id/dispute`. Date suggestions accept free text ("1962", "March 1962", "sometime in the 60s", …) parsed server-side.
- `POST /api/comments/:id/{hide,unhide}` — admin or moderator of any group the photo is in.
- `GET  /api/people[/:id][/autocomplete]`, `POST /api/people`, `POST /api/relationships` (files a suggestion).
- `GET  /api/albums[/:id]`.
- `POST /api/photos/:id/rescan_wanted` (admin).

Admin (`requireAdmin`):

- `GET  /api/admin/suggestions?status=&kind=`, `POST /api/admin/suggestions/:id/{accept,reject}` — 409 on a conflicting confirmed value; `force: true` overrides and audits. Accept refreshes completeness for the photo.
- `GET  /api/admin/disputes`, `POST /api/admin/faces/:id/resolve` (`action: keep|unassign|reassign`).
- `GET  /api/admin/audit`, `GET /api/admin/report/monthly?month=YYYY-MM`, `GET /api/admin/rescan-list`, `GET /api/admin/unfiled`.

Groups (visibility model):

- `GET  /api/groups[/:id]` — user's own groups (admin sees all).
- Admin: `POST /api/admin/groups`, `PATCH .../:id`, `POST .../:id/delete`, membership add/remove/role.
- Moderator (of `:groupId`): `POST /api/groups/:groupId/photos/:photoId/remove`, `POST .../members`, `POST .../members/:userId/remove`.
- Bulk assign: `POST /api/admin/photos/bulk-assign-groups` — synchronous, one transaction, cap 20 000 rows. Body `{album_id|scan_batch|person_id|year|decade|source_folder|ids, add: [gid], remove: [gid]}`.

Contributions (uploader own; admin/moderator across the whole set they scope to):

- `POST /api/contributions`; `HEAD /api/contributions/:id/files?sha256=` (204 if server holds); `POST /api/contributions/:id/files` (multipart, ≤100 MB); `POST /api/contributions/:id/finish` (emails admin); `GET /api/contributions/mine`.
- Admin/moderator: `GET /api/admin/contributions[/:id]`, `POST .../:id/files/:fid/{approve,reject}`, `POST .../:id/{approve-all,reject-all}` (admin batch). Moderator approve assigns only their group; moderator reject removes only their group from targets.

Sync (service token):

- `POST /sync/photos` (≤200, `need_files` handshake, 400 on private).
- `PUT  /sync/photos/:id/file`, `PUT /sync/photo_backs/:id/file`, `PUT /sync/faces/:id/crop`.
- Batched (≤500): `photo_masters`, `people`, `person_name_variants`, `relationships`, `places`, `photo_places`, `albums`, `album_photos`, `faces`, `photo_backs`, `suggestions`, `photo_groups`.
- `GET  /sync/pull/{groups,confirmed,contributions}?since=<ts>`; `GET .../contributions/:id/files/:file_id` (bytes); `POST .../contributions/:id/pulled`.
- `GET  /sync/status` — per-table counts + last-touch.

Visibility rule (implemented in `middleware/visibility.js`): admin sees
all non-private non-deleted photos including unfiled; contributor
needs a live shared group; `is_private` and `is_deleted` always
exclude. Non-members get 404 (never 403) — the media route, photo
detail, faces, backs, comments, likes, suggestions, and every list
and count go through the same fragment.

Minimal Phase 9 pages (replaced in Phase 10): `/upload` (mobile-first
sequential uploads with sha256 pre-check + progress + retry) and
`/admin/contributions` (per-file + batch approve/reject).

## Tests

Once, create the test DB (shared with `shared/`):

```
createdb -U postgres -O photo_user photoorg_test
```

Then before each `npm test` run:

```
npm run test:setup   # drops public schema, migrates up to head
npm test             # node --test with supertest
```

Test scenarios: request-access creates a row and one admin email; GET on
the approve token is inert; POST approves and emails the user; the magic
link's GET is inert; POST signs in; a second POST fails; expired links
fail; a suspended user's next login yields no magic link and their
session rows are gone; service token is accepted or rejected; the 6th
request within 15 min is 429.
