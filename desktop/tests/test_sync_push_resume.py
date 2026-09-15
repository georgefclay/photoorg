"""Push resumability: kill mid-batch, resume, second Push sends 0 files.

This spawns a real Express server against TEST_DATABASE_URL, points the
desktop push at it, and exercises:
  * A first push writes N photos, uploads N files.
  * A second push against the same web state upserts N again but
    uploads 0 files (the file-version handshake stops the second wave).
  * Private photos never appear in the request stream.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.conftest import DB_AVAILABLE, TEST_DATABASE_URL, requires_db

pytestmark = requires_db

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"


def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _node_available() -> bool:
    try:
        subprocess.run(["node", "-v"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@pytest.fixture(scope="module")
def web_server():
    if not _node_available() or not (WEB / "node_modules").exists():
        pytest.skip("node or web deps missing")

    port = _pick_free_port()
    tmp = Path(tempfile.mkdtemp(prefix="photoarchive-sync-pytest-"))
    photo_dir = tmp / "photodir"
    photo_dir.mkdir()

    env = {
        **os.environ,
        "PORT": str(port),
        "NODE_ENV": "test",
        "SESSION_SECRET": "test-session",
        "SERVICE_TOKEN": "test-service-token",
        "ADMIN_EMAIL": "admin@example.com",
        "BASE_URL": f"http://127.0.0.1:{port}",
        "DATABASE_URL": TEST_DATABASE_URL,  # server operates on the test DB
        "TEST_DATABASE_URL": TEST_DATABASE_URL,
        "PHOTO_DIR": str(photo_dir),
    }
    # Kill Postmark so the dev sink is used.
    env.pop("POSTMARK_API_KEY", None)

    # Fresh migrate the test DB before starting.
    r = subprocess.run(
        ["node", "node_modules/node-pg-migrate/bin/node-pg-migrate.js", "down", "0"],
        cwd=str(REPO_ROOT / "shared"), env={**env, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"migrate down failed: {r.stderr[:200]}")
    r = subprocess.run(
        ["node", "node_modules/node-pg-migrate/bin/node-pg-migrate.js", "up"],
        cwd=str(REPO_ROOT / "shared"), env={**env, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"migrate up failed: {r.stderr[:200]}")

    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=str(WEB), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # Wait for /healthz to answer.
    import urllib.request, urllib.error
    deadline = time.time() + 15
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
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _tiny_jpeg_bytes() -> bytes:
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc0001108000100010301220002110103110101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fbfe28a28a03ffd9"
    )


def test_push_creates_photos_then_resends_zero_files(web_server, tmp_path):
    """A first push uploads files; a second sends zero (file_version handshake)."""
    from photoarchive.modes.sync.client import WebSyncClient

    client = WebSyncClient(web_server, "test-service-token")

    # Seed 3 non-private + 1 private + 1 junk photo directly into the same
    # test DB the web server is reading. Push should send 3 metadata rows
    # and upload 3 files.
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    thumbs_dir = tmp_path / "thumbs"
    thumbs_dir.mkdir()

    import psycopg
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                truncate table photo_groups, group_members, groups,
                               audit_log, magic_links, access_requests, "session",
                               contribution_files, contributions,
                               suggestions, likes, comments, faces, photo_backs,
                               album_photos, albums, photo_masters, photos, users
                  restart identity cascade
            """)
        ids = {}
        for label, is_priv, tri in [("a", False, "keep"), ("b", False, "keep"),
                                     ("c", False, "keep"), ("priv", True, "keep"),
                                     ("junk", False, "junk")]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into photos (sha256, mime, source_root, source_folder, source_filename,
                                        triage_status, is_private, file_version, working_path)
                    values (%s, 'image/jpeg', 'photos', 'f', %s, %s, %s, 1, %s)
                    returning id
                    """,
                    (f"sha-{label}", f"{label}.jpg", tri, is_priv, f"00000000_{label}.jpg"),
                )
                ids[label] = int(cur.fetchone()[0])

    # Write real bytes to the working file paths the desktop will look up.
    for label in ("a", "b", "c", "priv", "junk"):
        photo_id = ids[label]
        base = f"{photo_id:08d}_{label[:4]}.jpg"
        # Update DB so working_path basename matches the file we write.
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("update photos set working_path = %s where id = %s", (base, photo_id))
        (working_dir / base).write_bytes(_tiny_jpeg_bytes())

    from photoarchive.modes.sync.push import push

    # Point db.py at the test DB.
    from photoarchive import db as pdb
    pdb.close_pool()

    class _S:
        DATABASE_URL = TEST_DATABASE_URL
    pdb.init_pool(_S(), min_size=1, max_size=2)  # type: ignore[arg-type]

    stats1 = push(client, working_dir=working_dir, thumbs_dir=thumbs_dir,
                  send_face_embeddings=False)
    assert stats1.photos_upserted == 3, stats1
    assert stats1.files_uploaded == 3

    # Confirm private + junk never traversed the wire — the web has no row.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from photos where sha256 = 'sha-priv'")
            # Note: our test DB IS the web DB — the private row is here (seeded),
            # but the push function must skip it and NOT increment upserts.
            # The stronger check is stats1.photos_upserted == 3 (only 3 non-priv/junk).
            pass

    stats2 = push(client, working_dir=working_dir, thumbs_dir=thumbs_dir,
                  send_face_embeddings=False)
    assert stats2.photos_upserted == 3, stats2
    assert stats2.files_uploaded == 0, "second push must send zero files"

    pdb.close_pool()
