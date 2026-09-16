# VM ops — Phase 14

Reference copies of the files installed on the AWS VM (Ubuntu, shared
with George's other sites) as of Phase 14. Real values (secrets,
paths, IPs) are placeholders here — the live copies are on the VM
in `/etc/systemd/system/`, `/etc/caddy/sites/`, `/etc/logrotate.d/`,
and in `~/photoorg/ops/`.

## Files

- `photoorg.service` — systemd unit; installed at `/etc/systemd/system/photoorg.service`.
- `cyberdinosaurs.com.caddy` — Caddy site; installed at `/etc/caddy/sites/cyberdinosaurs.com.caddy`.
- `photoorg.sudoers` — passwordless-restart rule; installed at `/etc/sudoers.d/photoorg` (chmod 440).
- `photoorg.logrotate` — weekly rotation for `/var/log/photoorg.log` and
  `/var/log/photoorg-cron.log`; installed at `/etc/logrotate.d/photoorg`.
- `pg_dump_photoorg.sh` — nightly Postgres dump script; installed at
  `~/photoorg/ops/pg_dump_photoorg.sh` (chmod 700).
- `cron-fragment.txt` — the block appended to `ubuntu`'s crontab.
  **Never install with `crontab cron-fragment.txt`** — the crontab is
  shared with several other sites. Use the fragmented sed pattern the
  mineralsmonitor site documents.

## Deploy runbook

```
ssh crafttags-vm
cd /home/ubuntu/photoorg
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_photoorg_deploy -o IdentitiesOnly=yes" \
  git pull --ff-only
(cd shared && npm ci && npm run migrate:up)
(cd web    && npm ci --omit=dev)
sudo systemctl restart photoorg.service
sudo systemctl status  photoorg.service --no-pager | head
curl -sf https://cyberdinosaurs.com/healthz && echo " ok"
```

## First-time provisioning

Not reproduced here — done once in Phase 14 (2026-09-16); see
`prompts/phase-14-deploy.md`, `PROJECT-PLAN.md`, and `GC.md` for the
audit trail. The deploy key must exist at `~/.ssh/id_ed25519_photoorg_deploy`
and be registered on `github.com/georgefclay/photoorg` as a read-only
deploy key before a git pull will work.
