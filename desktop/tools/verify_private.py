"""Verification step 3: prove `is_private=true` never reaches the web.

In this local setup the desktop and web share one Postgres database
(`photoorg`), so "absent from web DB" cannot literally hold — the row
exists in the shared DB. What we prove instead:

  1. The desktop's push selector never puts the private photo in a
     `/sync/photos` wire batch (measured by intercepting the client
     with a counter).
  2. `/sync/photos` returns 400 if we synthesise sending an is_private
     row from a foreign client.
  3. `/media/thumbs/:id` returns 404 while the photo is private.
  4. `/api/photos` list does not include the private photo (when
     called by an admin session — the strongest local privacy gate).

Then we unmark and re-push to prove the photo comes back.

Usage:
  python -m tools.verify_private --photo-id N --admin-email you@x.com
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg
import requests

from photoarchive import db
from photoarchive.config import load as load_settings
from photoarchive.modes.sync.client import WebSyncClient
from photoarchive.modes.sync.push import push


class CountingClient:
    """Wraps WebSyncClient to count which photo ids appear in each
    /sync/photos batch. Used to prove a private id never went on the
    wire."""

    def __init__(self, inner):
        self._inner = inner
        self.pushed_ids: set[int] = set()

    def push_photos(self, photos):
        for p in photos:
            self.pushed_ids.add(int(p["id"]))
        return self._inner.push_photos(photos)

    # Everything else delegates straight through.
    def __getattr__(self, name):
        return getattr(self._inner, name)


def _photo_row(cur, pid):
    cur.execute("select id, is_private, sha256, working_path, file_version from photos where id = %s", (pid,))
    return cur.fetchone()


def _paddedbase(pid: int) -> str:
    return f"{pid:08d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo-id", type=int, required=True)
    ap.add_argument("--photo-dir", default=os.environ.get("PHOTO_DIR", r"D:\Photos-web-local"))
    args = ap.parse_args()

    settings = load_settings()
    db.init_pool(settings, min_size=1, max_size=2)
    real_client = WebSyncClient(settings.WEB_API_URL, settings.WEB_API_TOKEN)

    try:
        pid = args.photo_id
        with db.connection() as conn:
            with conn.cursor() as cur:
                row = _photo_row(cur, pid)
        if not row:
            print(f"photo {pid} not found on laptop")
            return 2
        _, was_private, sha, wpath, fv = row
        if was_private:
            print(f"photo {pid} is already is_private=true; unmark first")
            return 3
        print(f"target: photo id={pid} sha={sha[:8]} v{fv}")
        _assert_web_has(pid, args.photo_dir, sha)
        print("baseline: web already has working + thumb -> OK")

        # --- flip to private ---
        with db.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("update photos set is_private = true where id = %s", (pid,))
        print(f"marked photo {pid} private on the laptop")

        # --- push while private ---
        counter = CountingClient(real_client)
        _push(counter, settings, "push with is_private=true")
        assert pid not in counter.pushed_ids, (
            f"privacy VIOLATION: id {pid} appeared in a /sync/photos wire batch"
        )
        print(f"  wire batches (n={len(counter.pushed_ids)}) do NOT include {pid} -> OK")

        # /sync/photos rejects a synthesized private row with 400.
        _assert_sync_rejects_private_row(pid, settings)
        print("  /sync/photos synthesized is_private=true -> 400 -> OK")

        # /media/thumbs/:id returns 404 (unauth call is 302 to /login,
        # so we use a service-token-less unauthenticated hit and expect
        # a redirect — the point is it doesn't serve the bytes).
        # Better: sign in as admin and try.
        _assert_media_thumb_404_for_admin(pid, settings)
        print(f"  /media/thumbs/{pid} for admin -> 404 while private -> OK")

        _assert_api_list_omits_private(pid, settings)
        print(f"  /api/photos list omits {pid} while private -> OK")

        # --- unmark and push again ---
        with db.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("update photos set is_private = false where id = %s", (pid,))
        print(f"unmarked photo {pid}")

        counter2 = CountingClient(real_client)
        _push(counter2, settings, "restore push")
        assert pid in counter2.pushed_ids, (
            f"expected id {pid} to appear on the wire after unmark, but it didn't"
        )
        _assert_web_has(pid, args.photo_dir, sha)
        print(f"  wire batches now include {pid}, files still present on web -> OK")

        print("VERIFY-PRIVATE: PASS")
        return 0
    finally:
        db.close_pool()


def _push(client, settings, label):
    print(f"--- {label} ---")
    stats = push(
        client,
        working_dir=Path(settings.WORKING_DIR),
        thumbs_dir=Path(settings.THUMBS_DIR),
        send_face_embeddings=False,
    )
    print(f"  photos_upserted={stats.photos_upserted}, files_uploaded={stats.files_uploaded}, bytes={stats.bytes_uploaded}")


def _assert_web_has(pid, photo_dir, sha):
    working = Path(photo_dir) / "working" / f"{_paddedbase(pid)}_{sha[:8]}.jpg"
    thumb = Path(photo_dir) / "thumbs" / f"{_paddedbase(pid)}.jpg"
    assert working.exists(), f"web working missing: {working}"
    assert thumb.exists(),   f"web thumb missing:   {thumb}"


def _assert_sync_rejects_private_row(pid, settings):
    r = requests.post(
        f"{settings.WEB_API_URL.rstrip('/')}/sync/photos",
        headers={"Authorization": f"Bearer {settings.WEB_API_TOKEN}"},
        json={"photos": [{
            "id": pid, "sha256": "x", "mime": "image/jpeg", "file_version": 1,
            "source_root": "photos", "source_folder": "f", "source_filename": "a.jpg",
            "is_private": True,
        }]},
        timeout=30,
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"
    assert "private" in r.text.lower(), r.text


def _admin_session(settings, admin_email: str | None = None):
    """Approve -> sign in flow, returns a `requests.Session` cookie jar."""
    email = admin_email or os.environ.get("ADMIN_EMAIL", "georgefclay@gmail.com")
    with psycopg.connect(settings.DATABASE_URL) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("select id from users where lower(email) = lower(%s) and role = 'admin'", (email,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"admin {email} not found in users")
            admin_id = row[0]
            import secrets, hashlib
            token = secrets.token_hex(32)
            th = hashlib.sha256(token.encode()).hexdigest()
            cur.execute(
                "insert into magic_links (user_id, token_hash, expires_at) values (%s, %s, now() + interval '5 minutes')",
                (admin_id, th),
            )
    s = requests.Session()
    r = s.post(f"{settings.WEB_API_URL.rstrip('/')}/a/{token}", timeout=10, allow_redirects=False)
    assert r.status_code == 302, f"admin signin: expected 302, got {r.status_code}"
    return s


def _assert_media_thumb_404_for_admin(pid, settings):
    s = _admin_session(settings)
    r = s.get(f"{settings.WEB_API_URL.rstrip('/')}/media/thumbs/{pid}", timeout=10, allow_redirects=False)
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:200]}"


def _assert_api_list_omits_private(pid, settings):
    s = _admin_session(settings)
    r = s.get(f"{settings.WEB_API_URL.rstrip('/')}/api/photos?limit=100", timeout=10)
    assert r.status_code == 200, r.text[:200]
    ids = {item["id"] for item in r.json().get("items", [])}
    assert pid not in ids, f"private photo {pid} showed up in /api/photos list"


if __name__ == "__main__":
    sys.exit(main())
