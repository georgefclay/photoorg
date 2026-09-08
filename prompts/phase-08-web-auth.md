# Phase 8 — Web: authentication

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§2 decisions incl. the CraftTags lessons, Phase 8), `shared/SCHEMA.md` (users, access_requests, magic_links, session, audit_log), and `photo-archive-build-prompts.md` §7. Work in `web/`. The Phase 0 stub (`server.js`, `views/home.ejs`) exists. Stack is **Express + EJS + plain JS**, no React, no build step. Same conventions as George's other sites.

GOAL: no-signup authentication. Strangers request access; George approves from an email or an admin page; approved users sign in with magic links; sessions are long-lived cookies stored in Postgres. A service token authenticates the desktop app.

## Non-negotiables (from CraftTags in production)
- `app.set('trust proxy', 1)` before session middleware (Caddy terminates TLS).
- **Every expiry is computed in SQL**: `expires_at = now() + interval '15 minutes'`. Never a JS `Date`.
- **Token links never act on GET.** `GET /a/:token` renders a page with one button; `POST /a/:token` does the work. Mail scanners prefetch links.
- Specific routes before wildcards. No wildcard routes in this phase anyway.
- Rate limit `POST /request-access` and `POST /login` (express-rate-limit, 5/15 min per IP) and add a honeypot field to both forms.
- Never reveal whether an email exists: `POST /login` always says "if that address has access, a link is on its way".

## Flows
**Request access** — `GET /request-access` form (email, optional message, honeypot). `POST` inserts `access_requests` (status pending, `token` = 32 random bytes hex, `token_expires_at = now() + interval '72 hours'`), emails **only** `ADMIN_EMAIL` with the message and two links: `/admin/access/:token/approve` and `/admin/access/:token/deny`. The requester gets nothing but a "thanks, you'll hear back" page. Duplicate pending request for the same email → same page, no second email (log it).

**Admin decision** — the two links each render a confirm page (GET), act on POST. Approve: create `users` row (role contributor, status active) if absent, mark request approved with `decided_by` = admin, send the new user their first magic link. Deny: mark denied, no email to the requester. Expired or used token → a plain "this link has expired" page. Also `GET /admin/access` lists pending requests with Approve/Deny buttons (POST) for when the email is lost.

**Magic link login** — `GET /login` form (email). `POST` : if a user with that email is `active`, insert `magic_links` (store `token_hash` = sha256 of the token, `expires_at = now() + interval '15 minutes'`), email the link `/a/:token`. `GET /a/:token` → confirm page ("Sign in as x@y — Continue"). `POST /a/:token` → verify hash, unexpired, `used_at is null`, user still active → set `used_at`, regenerate session, set `req.session.userId`, update `last_login_at`, redirect to `/`. Single-use.

**Sessions** — `express-session` + `connect-pg-simple` on the existing `session` table. Cookie: httpOnly, sameSite lax, secure in production, `maxAge` 180 days. Session row stores only `userId`. Middleware `loadUser` fetches the user on each request and **refuses if status is not active** (that is how suspension takes effect immediately — no session purge needed, but also delete the user's session rows on suspend for tidiness).

**Roles** — `requireUser`, `requireAdmin` middleware. Admin routes under `/admin`.

**Admin: users** — `GET /admin/users` list; POST actions: suspend, reactivate, promote to admin, demote. Suspending keeps every contribution; only access changes. Each action writes `audit_log` (actor = admin email, entity users, previous/new status or role).

**Service account** — `SERVICE_TOKEN` in `.env`. Middleware `requireService` checks `Authorization: Bearer <token>` (constant-time compare) and sets `req.service = true`. No `users` row; never appears in lists. Used by Phase 9 sync routes.

**Bootstrap** — `node tools/create-admin.js <email>` inserts the first admin. No admin can be created any other way.

## Email
`services/email.js` like CraftTags: Postmark when `POSTMARK_API_KEY` is set, otherwise log the full message to the console **and** write it to `web/tmp/mail/<timestamp>.txt` so the links can be clicked during dev. Templates in `views/email/*.ejs` (text and HTML). From `POSTMARK_FROM_EMAIL`. Subject prefixes `[Photo Archive]`.

## Pages
Minimal, mobile-first, one shared layout (`views/layout.ejs` with a header showing signed-in email and Sign out). Pages: home (placeholder for now: signed-in users see "archive coming", strangers see Request access / Sign in), request-access, request-access-sent, login, login-sent, confirm-token, expired, admin/access, admin/users, error. Plain CSS in `public/css/site.css`. No frameworks.

## Audit
Every state change: access request created/approved/denied, user created/suspended/reactivated/role change, magic link redeemed (login). `actor` is the acting user's email, `system` for automatic steps.

## Tests
`node --test` with supertest against `TEST_DATABASE_URL` (migrate up once; truncate between tests). Cover: request-access creates a row and one admin email; GET on approve token changes nothing; POST approves and emails the user; magic link GET changes nothing, POST signs in, second POST fails; expired link fails; suspended user's next request is refused and their session is gone; service token accepted / rejected; rate limit trips on the 6th request.

## Verification, then stop
1. `npm test` green.
2. Manual run with `create-admin.js`, request access from a second address, approve via the logged email, sign in with the resulting magic link; paste the audit rows.
3. `web/README.md` documents env vars, bootstrap, and the dev-mail folder. `CLAUDE.md` gets the auth rules.
4. Commit: `Phase 8: web auth`.

---

## Answers to Claude Code's questions

1. `BASE_URL` (already in `web/.env.example` from Phase 0). Dev default `http://localhost:${PORT}`. Domain still undecided; nothing in this phase depends on it.
2. Confirmed: `ADMIN_EMAIL` is one string; other admins are not copied. (Multi-admin notification can come later if ever needed.)
3. Yes.
4. (b) — per-session CSRF token on every authed POST form, checked by middleware. Token-link POST pages are exempt (unguessable token is the CSRF defence there).
5. Confirmed: leave `is_service` unused.
6. Distinct welcome template and subject.
7. Capture `display_name` on the request-access form (optional, "what should we call you?"). Fallback to the part of the email before `@`.
8. Correct: no auto-send.
9. OK.
10. Silent fake success. Log it at info with the IP.
11. OK, with two additions: `auth.magic_link.sent` and `auth.magic_link.expired_attempt` (both without user enumeration in the response, but useful in the log).
12. Leave everything. Revisit if the tables grow.
13. `npm run test:setup` migrates `TEST_DATABASE_URL` once; tests truncate between cases. Refuse if it equals `DATABASE_URL` (same guard as `shared/`).
14. Yes, real limiter, hammered in-process.
15. Exclude static and `/healthz`.
16. One page per action.

GO.
