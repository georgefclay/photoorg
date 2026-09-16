# Phase 14 — Deploy to the AWS VM (moved up: right after Phase 9)

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§2 Hosting, Domain and email, Groups; Phase 14), `web/README.md`, `shared/README.md`, and `GC.md` (gitignored — VM host, SSH alias, existing conventions). The VM already hosts several of George's sites with **Caddy → systemd Node service → Postgres 18**, deployed by `git pull` + `systemctl restart`. Follow that pattern exactly. Reference: the CraftTags site on the same box (`/home/ubuntu/crafttags`, `crafttags.service`, `/etc/caddy/sites/crafttags.caddy`).

GOAL: `https://cyberdinosaurs.com` serves the web app from the VM, with its own database and Postmark sender, the desktop can Sync to it, and backups exist. Everything you do on the VM is written into `GC.md` as you go (commands, paths, unit names) so George can operate it without you.

You have SSH from the laptop (`ssh crafttags-vm`, key in `~/.ssh`). Use it. Never store secrets in the repo; the VM's `.env` is created by hand over SSH. Ask George before anything destructive or costly (disk resize, DNS).

## 1. Pre-flight (report, don't change)
- `df -h`, free space on the main disk; `free -m`; `node --version`; `psql --version`; Caddy version; list existing sites in `/etc/caddy/sites/`.
- Estimate the working-set footprint: sum of `photos.file_size` for the keep set on the laptop + thumbs + faces + backs (ask the desktop DB). Report it against free space. **If the disk needs enlarging, stop and tell George the size to grow to; he does it in the AWS console, then `growpart`/`resize2fs` over SSH with his approval.**
- Confirm DNS: `cyberdinosaurs.com` A record → VM IP. If absent, tell George exactly what record to add at his registrar and wait.

## 2. Database
- As `postgres`: `CREATE ROLE photoorg LOGIN PASSWORD '<generate 32 alphanumeric>'; CREATE DATABASE photoorg OWNER photoorg;` (alphanumeric only — no shell/URL escaping problems). Print the password once; George puts it in GC.md.
- Clone the repo to `/home/ubuntu/photoorg` (deploy key like the other sites), `cd shared && npm ci && DATABASE_URL=... npm run migrate:up`. Verify `select count(*) from pgmigrations` equals the laptop's.
- Nickname seed: `node seed/nicknames.js`.

## 3. Web service
- `/home/ubuntu/photoorg/web`: `npm ci --omit=dev`. Create `.env` (chmod 600): `PORT=8091` (or the next free port — check the other units), `NODE_ENV=production`, `BASE_URL=https://cyberdinosaurs.com`, `DATABASE_URL`, `SESSION_SECRET` (new), `SERVICE_TOKEN` (new; George copies it to `desktop/.env` `WEB_API_TOKEN`), `PHOTO_DIR=/srv/photoorg` (create; owner `ubuntu`; on the main disk), `ADMIN_EMAIL=georgefclay@gmail.com`, `POSTMARK_API_KEY` + `POSTMARK_FROM_EMAIL=archive@cyberdinosaurs.com` (George supplies the key; leave blank until then — dev-mail mode).
- `photoorg.service` systemd unit modelled on `crafttags.service`: `WorkingDirectory=/home/ubuntu/photoorg/web`, `ExecStart=/usr/local/bin/node server.js`, `Restart=always`, logs to `/var/log/photoorg.log`. `LimitNOFILE` raised. Enable + start. Add the passwordless-sudo restart rule like the others.
- `/etc/caddy/sites/photoorg.caddy`: `cyberdinosaurs.com, www.cyberdinosaurs.com` → `reverse_proxy localhost:8091`, `request_body { max_size 120MB }` (uploads), `encode gzip`. `sudo systemctl reload caddy`. Confirm the cert issues and `https://cyberdinosaurs.com/healthz` returns 200.
- Bootstrap admin: `node tools/create-admin.js georgefclay@gmail.com`.

## 4. Mail
- George: in Postmark, add sender signature/domain `cyberdinosaurs.com`, add the SPF and DKIM records it gives, verify. Then put the API key in the VM `.env` and restart. Test: request access from a second address on the live site; the admin email should arrive in George's inbox; approve; the welcome magic link arrives at the second address. (This is the phone test we skipped in Phase 9 — do it from the phone.)

## 5. Sync from the laptop
- Desktop `.env`: `WEB_API_URL=https://cyberdinosaurs.com`, `WEB_API_TOKEN=<SERVICE_TOKEN>`. Run `tools.check_working_files` first.
- Full **Push**. Report elapsed and bytes — this is ~12,800 photos over the internet, so expect an hour or more; it is resumable, so a dropped connection just means pressing Push again.
- Second Push sends 0 files. `GET /sync/status` counts match the laptop.
- Create the first group on the site ("Clay Family"), add George; bulk-assign a small set (e.g. one album) from the desktop Groups panel; push; confirm the photos appear for a contributor account on the phone.

## 6. Ops hardening (all recorded in GC.md)
- Nightly `pg_dump photoorg | gzip > /home/ubuntu/backups/photoorg-$(date +%F).sql.gz`, rotate 14 days, cron 03:20. Also a weekly `rsync`/`tar` of `/srv/photoorg` to wherever George's other backups go — ask him; if nowhere yet, note it as a TODO with the S3 option.
- fail2ban: whitelist George's current IP per the CraftTags note; remember the 4xx jail when smoke-testing.
- UptimeRobot (or the same monitor as the other sites) on `https://cyberdinosaurs.com/healthz`.
- Log rotation for `/var/log/photoorg.log`.
- Deploy runbook in GC.md: `ssh crafttags-vm && cd /home/ubuntu/photoorg && git pull && (cd shared && npm ci && npm run migrate:up) && (cd web && npm ci --omit=dev) && sudo systemctl restart photoorg`.

## Verification, then stop
1. `https://cyberdinosaurs.com` loads over TLS; `/healthz` 200; Caddy access log shows the request.
2. Request-access → approve → magic-link sign-in works end to end with real email, from a phone off the home Wi-Fi.
3. Full push done; second push 0 files; `/sync/status` matches.
4. Contributor on a phone sees exactly the photos in their group and nothing else; unfiled photos invisible to them.
5. Backup file exists in `/home/ubuntu/backups/`; monitor is green.
6. `GC.md` has every command, path, unit, port, and secret location. `PROJECT-PLAN.md` gets a progress note from the PM.
7. Commit anything that changed in the repo (Caddy/systemd templates go in `ops/vm/` as examples with placeholders, never real secrets): `Phase 14: deploy to VM`.

---

## Answers to Claude Code's questions

### Pre-flight facts (2026-09-15)

- **Host**: `ubuntu@44.255.253.62` — Ubuntu 26.04 aarch64, Node 24.16, PostgreSQL 18.6, Caddy 2.11.4. `crafttags.service` and 30+ other sites share this box.
- **Disk**: `/dev/root` — 30 GB total, **19 GB free (39% used)**.
- **RAM**: 1.8 GiB total, 104 Mi free + 761 Mi buff/cache, 2 GiB swap (~570 Mi used).
- **DNS**: `cyberdinosaurs.com` → `44.255.253.62` already resolves. No registrar step needed.
- **Ports free** near the pattern: **8094**, 8081, 8082, 8084, 8096+. 8091 is already taken (a MainThread process).
- **Site convention (from `crafttags`)**: `/home/ubuntu/<name>/` for code, `<name>.git` bare repo at the same level, systemd unit `<name>.service`, log at `/var/log/<name>.log`, Caddy site file `<domain>.caddy` in `/etc/caddy/sites/`, redirect `www` → apex, `encode zstd gzip`, `header { Strict-Transport-Security … }`. `.env` in the app dir.
- **Cron convention**: `ubuntu`'s crontab is shared with other apps — the mineralsmonitor comment on the box warns "NEVER install with `crontab ops/cron.txt`". Append via the fragmented sed pattern.
- **Laptop archive footprint**: working ≈ **28 GB**, thumbs 274 MB. Backs (826) and face crops (23 461) aren't uploaded yet — push.py only PUTs photo working files, not backs/faces. Adding those: backs ~1–2 GB, face crops (256 px JPEG) ~0.7 GB.

### Questions

1. **Disk needs enlarging before the full push.**  My phase-9 push moved 27.29 GB of working copies; add thumbs + backs + face crops + DB + Postgres WAL + logs + the other 30 sites already living here, and 30 GB isn't enough. Recommend growing the root volume to **80 GB** — comfortable for a few years of new scans and contributions on top of today's archive, and cheap on AWS. Confirm the target size and I'll wait while you resize in the AWS console, then run `growpart` + `resize2fs` over SSH with your OK.

2. **Deploy path**. Following the `crafttags` pattern: `/home/ubuntu/photoorg/` for code, `/home/ubuntu/photoorg.git` for the bare repo, and `/srv/photoorg/` for the working-set (Photo files) with subfolders `working/ thumbs/ backs/ faces/ uploads/`. Owner `ubuntu`. OK?

3. **Port**. `8094` (checked; free). OK?

4. **DB**. Role name `photoorg` (matches the DB name), password 32 alphanumeric auto-generated on the VM. Printed once; you copy it into GC.md. OK?

5. **Deploy key**. I'll generate an ed25519 key on the VM at `~/.ssh/id_ed25519_photoorg_deploy` and print the public part; you add it as a deploy key on `georgefclay/photoorg` on GitHub. Read-only. OK?

6. **SERVICE_TOKEN**. Fresh 32-byte URL-safe token, VM-side. I'll print it and update `desktop/.env` on the laptop to match, then update `web/.env` on the VM. The Phase 9 dev token stays in the laptop-local `web/.env` for local dev.

7. **Postmark**. Deploy today in **dev-mail mode** (`POSTMARK_API_KEY=` blank; sink under `web/tmp/mail/`), you add the `cyberdinosaurs.com` sender signature + SPF/DKIM at your convenience, and I flip the key once verified. OK, or do you want to wire Postmark before the first push?

8. **Nightly `pg_dump`**. Install a fragment in `~ubuntu`'s crontab following the mineralsmonitor "NEVER replace" pattern: `20 3 * * * pg_dump ... | gzip > /home/ubuntu/backups/photoorg-$(date +%F).sql.gz` with a 14-day retention. Off-site backup deferred (note as a TODO). OK?

9. **Uptime monitoring**. UptimeRobot on `https://cyberdinosaurs.com/healthz` — is that on your existing UptimeRobot account (I'll give you the URL to add) or do you want a different monitor?

10. **fail2ban**. The other sites here have fail2ban whitelisting a specific IP. What's yours (current) that I should whitelist before I hit the site from this laptop with a burst of push traffic?

11. **Full push scheduling**. The push over the internet at ~5 Mbps up (typical residential) for 28 GB is 12–18 hours. Two options: (a) run it start-to-finish now and monitor, (b) start it and let the resumable protocol handle interruptions across a few sessions. Preference?

Given the go-ahead I'll: do steps 1–4 (DB, code, systemd, Caddy) → confirm cert issues → bootstrap admin → then wait on your GO before starting the push in step 5.

### PM answers (2026-09-15)

**Scope change: deploy the site, not the archive.** The mini is still running classify/describe/estimate_date, face labelling is ongoing, and Phase 7 (scan cleanup) will replace working copies for accepted scans and bump `file_version`. A full 28 GB push now would be largely re-pushed later. So in this phase:
- Push **metadata only** for everything (photos rows, people, faces without embeddings, backs/transcriptions, suggestions, albums, places) — small, and it lets the site's people/search pages be exercised.
- Push **files** only for one test group: create "Clay Family", bulk-assign one small album (≤ 200 photos) from the desktop Groups panel, and push just those files. Add a **"Files: only photos in a group"** option to Push (default on until the full push is scheduled) so the desktop never accidentally sends the whole set.
- The full file push is a separate step, scheduled after Phase 7 and the mini's jobs finish. Note it in PROJECT-PLAN and GC.md.

1. **Disk**: George decides — resize to 80 GB now (one-time console click, then `growpart`/`resize2fs` with his OK) or leave at 30 GB until the full push. Ask him; either is fine for this phase.
2. **Paths**: yes, as proposed.
3. **Port**: 8094.
4. **DB**: yes.
5. **Deploy key**: yes, read-only.
6. **SERVICE_TOKEN**: yes. Keep the local dev token separate.
7. **Postmark**: George has already set up the sender domain and DNS. Wire it now: ask him for the API key, put it in the VM `.env` only, restart, and run the request-access → approve → magic-link test with real mail (from his phone, off Wi-Fi).
8. **pg_dump cron**: yes, fragment pattern, 14-day retention. Off-site: note as TODO with the S3 option.
9. **Monitoring**: existing UptimeRobot account; give George the URL to add.
10. **fail2ban**: ask George for his current public IP (`curl -4 ifconfig.me` from the laptop) and whitelist it.
11. **Push scheduling**: moot for now (metadata + one group only). When the full push happens later, (b) resumable across sessions.

GO.

