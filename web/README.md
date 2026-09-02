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
