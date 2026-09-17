"""Phase 9 fix-up 1 — web-origin ids, end to end with two databases.

The desktop side is `public` in TEST_DATABASE_URL (desktop migrations, no
floor). The web side is a separate schema, `websync`, in the same test
database, migrated with PHOTOORG_DB_ROLE=web so its sequences start at
WEB_ID_FLOOR; the web server runs with `search_path=websync,public`.
That gives two independent sets of tables with overlapping low ids
without needing a second database (photo_user can't CREATEDB).

Proves:
  * a web insert lands >= floor;
  * a desktop push of an id >= floor is refused (400);
  * pull round-trips a web-drawn face (crop cut, embedding stale) and a
    web-created person, with their web ids;
  * rescan_wanted set on the web survives a push (push pulls first);
  * a suggestion accepted on the web isn't re-opened by the next push;
  * the desktop's own sequences stay below the floor.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"
SHARED = REPO_ROOT / "shared"
MIGRATE = ["node", "node_modules/node-pg-migrate/bin/node-pg-migrate.js"]
WEB_SCHEMA = "websync"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _node_ok() -> bool:
    try:
        subprocess.run(["node", "-v"], check=True, capture_output=True)
        return (WEB / "node_modules").exists() and (SHARED / "node_modules").exists()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _with_search_path(url: str) -> str:
    opt = quote(f"-c search_path={WEB_SCHEMA},public", safe="")
    return f"{url}{'&' if '?' in url else '?'}options={opt}"


def _web_conn():
    import psycopg
    conn = psycopg.connect(TEST_DATABASE_URL, options=f"-c search_path={WEB_SCHEMA},public")
    conn.autocommit = True
    return conn


def _desk_conn():
    import psycopg
    conn = psycopg.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def two_sided():
    if not _node_ok():
        pytest.skip("node or web/shared deps missing")
    base_env = {k: v for k, v in os.environ.items() if k != "PHOTOORG_DB_ROLE"}

    # Desktop side: public, no floor.
    for step in (["down", "0"], ["up"]):
        r = subprocess.run(MIGRATE + step, cwd=str(SHARED), capture_output=True, text=True,
                           env={**base_env, "DATABASE_URL": TEST_DATABASE_URL})
        if r.returncode != 0:
            pytest.skip(f"desktop migrate {step} failed: {r.stderr[-300:]}")

    # Web side: fresh schema, migrated as a web DB.
    with _desk_conn() as c:
        c.execute(f"drop schema if exists {WEB_SCHEMA} cascade")
        c.execute(f"create schema {WEB_SCHEMA}")
    r = subprocess.run(
        MIGRATE + ["up", "-s", WEB_SCHEMA, "-s", "public", "--migrations-schema", WEB_SCHEMA],
        cwd=str(SHARED), capture_output=True, text=True,
        env={**base_env, "DATABASE_URL": TEST_DATABASE_URL, "PHOTOORG_DB_ROLE": "web"},
    )
    if r.returncode != 0:
        pytest.skip(f"web-schema migrate failed: {r.stderr[-300:]}")

    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="photoarchive-idfloor-"))
    photo_dir = tmp / "web-photodir"
    photo_dir.mkdir()
    env = {
        **base_env,
        "PORT": str(port), "NODE_ENV": "test",
        "SESSION_SECRET": "test-session", "SERVICE_TOKEN": "test-service-token",
        "ADMIN_EMAIL": "admin@example.com", "BASE_URL": f"http://127.0.0.1:{port}",
        "DATABASE_URL": _with_search_path(TEST_DATABASE_URL),
        "TEST_DATABASE_URL": TEST_DATABASE_URL,
        "PHOTO_DIR": str(photo_dir),
    }
    env.pop("POSTMARK_API_KEY", None)
    proc = subprocess.Popen(["node", "server.js"], cwd=str(WEB), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r2:
                if r2.status == 200:
                    break
        except (urllib.error.URLError, ConnectionResetError, ConnectionRefusedError):
            time.sleep(0.2)
    else:
        proc.terminate()
        pytest.skip("web /healthz never came up")
    try:
        yield {"url": f"http://127.0.0.1:{port}", "tmp": tmp}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        with _desk_conn() as c:
            c.execute(f"drop schema if exists {WEB_SCHEMA} cascade")
        shutil.rmtree(tmp, ignore_errors=True)


def _login(base: str, conn, user_id: int):
    import requests
    token = f"tok-{user_id}-{time.time_ns()}"
    conn.execute(
        """insert into magic_links (user_id, token_hash, expires_at)
           values (%s, encode(sha256(%s::bytea), 'hex'), now() + interval '15 minutes')""",
        (user_id, token.encode()),
    )
    s = requests.Session()
    r = s.post(f"{base}/a/{token}", allow_redirects=False)
    assert r.status_code in (302, 303), r.text[:200]
    s.headers["X-CSRF-Token"] = s.get(f"{base}/api/csrf").json()["csrfToken"]
    return s


def test_web_origin_ids_round_trip(two_sided):
    from PIL import Image

    from photoarchive import db as pdb
    from photoarchive.id_ranges import WEB_ID_FLOOR
    from photoarchive.modes.sync.client import WebSyncClient, WebSyncError
    from photoarchive.modes.sync.push import push

    base = two_sided["url"]
    tmp = two_sided["tmp"]
    working_dir, thumbs_dir, state_dir = tmp / "working", tmp / "thumbs", tmp / "sync-state"
    working_dir.mkdir()
    thumbs_dir.mkdir()
    client = WebSyncClient(base, "test-service-token")

    # ---- Desktop seed: low ids --------------------------------------
    with _desk_conn() as c:
        photo_id = c.execute(
            """insert into photos (sha256, mime, source_root, source_folder, source_filename,
                                   triage_status, file_version, width, height)
               values ('sha-floor', 'image/jpeg', 'photos', 'f', 'a.jpg', 'keep', 1, 800, 600)
               returning id""").fetchone()[0]
        wname = f"{photo_id:08d}_shaflr00.jpg"
        Image.new("RGB", (800, 600), (120, 90, 60)).save(working_dir / wname, "JPEG")
        c.execute("update photos set working_path = %s where id = %s", (str(working_dir / wname), photo_id))
        person_id = c.execute("insert into people (given_name) values ('Peggy') returning id").fetchone()[0]
        c.execute("""insert into faces (photo_id, person_id, bbox, source)
                     values (%s, %s, '{"x":10,"y":10,"w":80,"h":80}', 'ai')""", (photo_id, person_id))
        Image.new("RGB", (600, 400), (240, 240, 230)).save(working_dir / "back_0001.jpg", "JPEG")
        back_id = c.execute(
            """insert into photo_backs (photo_id, master_path, sha256, working_path, transcribed_text)
               values (%s, 'D:/Scanned Photos/B/0002.jpg', 'backsha123456', %s, 'Easter 1962')
               returning id""", (photo_id, str(working_dir / "back_0001.jpg"))).fetchone()[0]
        sug_id = c.execute(
            """insert into suggestions (photo_id, kind, payload, source, status, model)
               values (%s, 'date', '{"date":"1971-01-01","precision":"year"}', 'ai', 'pending', 'm')
               returning id""", (photo_id,)).fetchone()[0]
    assert person_id < WEB_ID_FLOOR and sug_id < WEB_ID_FLOOR

    pdb.close_pool()

    class _S:
        DATABASE_URL = TEST_DATABASE_URL
    pdb.init_pool(_S(), min_size=1, max_size=3)  # type: ignore[arg-type]
    try:
        first = push(client, working_dir=working_dir, thumbs_dir=thumbs_dir, state_dir=state_dir,
                     files_only_for_grouped=False,
                     allow_shared_db=True)  # same DB, separate schemas (see module docstring)
        # Back images travel too (as JPEG under a name derived from id + sha).
        assert first.tables["back_files"] == 1, first.tables
        assert (tmp / "web-photodir" / "backs" / f"back_{back_id:08d}_backsha1.jpg").exists()

        # ---- Web side: people, group, contributor tag, admin accepts ----
        with _web_conn() as w:
            assert w.execute("select count(*) from faces").fetchone()[0] == 1
            admin = w.execute("""insert into users (email, role, status) values ('admin@example.com', 'admin', 'active')
                                 returning id""").fetchone()[0]
            alice = w.execute("""insert into users (email, role, status) values ('alice@example.com', 'contributor', 'active')
                                 returning id""").fetchone()[0]
            g = w.execute("insert into groups (name) values ('Clay Family') returning id").fetchone()[0]
            w.execute("insert into group_members (group_id, user_id) values (%s, %s)", (g, alice))
            w.execute("insert into photo_groups (photo_id, group_id) values (%s, %s)", (photo_id, g))
            alice_s = _login(base, w, alice)
            admin_s = _login(base, w, admin)

        tag = alice_s.post(f"{base}/api/photos/{photo_id}/faces", json={
            "bbox": {"x": 400, "y": 100, "w": 120, "h": 120},
            "new_person": {"given_name": "Lola", "surname": "Clay"},
        })
        assert tag.status_code == 201, tag.text
        web_face_id = tag.json()["face_id"]
        assert web_face_id >= WEB_ID_FLOOR
        assert tag.json()["suggestion_id"] >= WEB_ID_FLOOR

        acc = admin_s.post(f"{base}/api/admin/suggestions/{tag.json()['suggestion_id']}/accept", json={})
        assert acc.status_code == 200, acc.text
        web_person_id = acc.json()["applied"]["person_id"]
        assert web_person_id >= WEB_ID_FLOOR

        acc_ai = admin_s.post(f"{base}/api/admin/suggestions/{sug_id}/accept", json={})
        assert acc_ai.status_code == 200, acc_ai.text
        rescan = admin_s.post(f"{base}/api/photos/{photo_id}/rescan_wanted", json={"wanted": True})
        assert rescan.status_code == 200, rescan.text

        # A desktop face created after the web's: overlapping-range check.
        with _desk_conn() as c:
            local_face = c.execute("""insert into faces (photo_id, bbox, source)
                                      values (%s, '{"x":600,"y":10,"w":50,"h":50}', 'ai') returning id""",
                                   (photo_id,)).fetchone()[0]
        assert local_face < WEB_ID_FLOOR

        # ---- Push (pulls first) -----------------------------------------
        stats = push(client, working_dir=working_dir, thumbs_dir=thumbs_dir, state_dir=state_dir,
                     files_only_for_grouped=False,
             allow_shared_db=True)  # same DB, separate schemas (see module docstring)
        assert stats.tables["faces"] == 2, stats.tables  # desktop faces only
        assert stats.tables["back_files"] == 0, "second push re-sends no back images"

        with _desk_conn() as c:
            f = c.execute("""select person_id, source, embedding, embedding_stale, bbox
                               from faces where id = %s""", (web_face_id,)).fetchone()
            assert f is not None, "web face pulled down with its web id"
            assert f[0] == web_person_id and f[1] == "human"
            assert f[2] is None and f[3] is True
            assert f[4] == {"x": 400, "y": 100, "w": 120, "h": 120}
            p = c.execute("select given_name, surname from people where id = %s", (web_person_id,)).fetchone()
            assert p == ("Lola", "Clay")
            assert c.execute("select rescan_wanted from photos where id = %s", (photo_id,)).fetchone()[0] is True
            assert c.execute("select status from suggestions where id = %s", (sug_id,)).fetchone()[0] == "accepted"
            # Desktop sequences untouched by the pulled explicit ids.
            nxt = c.execute("""insert into people (given_name) values ('Later') returning id""").fetchone()[0]
            assert nxt < WEB_ID_FLOOR
        assert (thumbs_dir / "faces" / f"{web_face_id}.jpg").exists(), "face crop cut at pull"

        with _web_conn() as w:
            assert w.execute("select rescan_wanted from photos where id = %s", (photo_id,)).fetchone()[0] is True, \
                "rescan_wanted set on the web survives the push"
            assert w.execute("select status from suggestions where id = %s", (sug_id,)).fetchone()[0] == "accepted"
            ids = [r[0] for r in w.execute("select id from faces order by id").fetchall()]
            assert ids == sorted([1, local_face, web_face_id]), ids
            assert w.execute("select person_id from faces where id = %s", (web_face_id,)).fetchone()[0] == web_person_id

        # ---- A push of an id at/above the floor is refused --------------
        with pytest.raises(WebSyncError, match="HTTP 400"):
            client.push_batch("faces", [{
                "id": WEB_ID_FLOOR + 99, "photo_id": photo_id,
                "bbox": {"x": 1, "y": 1, "w": 5, "h": 5}, "source": "ai",
            }])
        with pytest.raises(WebSyncError, match="HTTP 400"):
            client.push_batch("people", [{"id": web_person_id, "given_name": "Overwrite"}])
        with _web_conn() as w:
            assert w.execute("select given_name from people where id = %s", (web_person_id,)).fetchone()[0] == "Lola"

        status = client.status()
        assert status["id_floor"]["ok"] is True
    finally:
        pdb.close_pool()
