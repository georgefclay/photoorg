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
| `SERVICE_TOKEN` | Bearer token the desktop app presents on sync endpoints (Phase 9 onwards). Constant-time compared. |
| `POSTMARK_API_KEY` | Leave blank in dev to use the dev-mail fallback. |
| `POSTMARK_FROM_EMAIL` | Verified sender in Postmark. |
| `PHOTO_DIR` | Where working-copy JPEGs live on this host. |

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
