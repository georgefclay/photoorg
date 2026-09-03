"""Shared pytest fixtures. Loads DB URLs from ../shared/.env (single source
of truth) — matches how Phase 1's Node smoke test resolves them."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED_ENV = _REPO_ROOT / "shared" / ".env"


def _load_shared_env() -> dict[str, str]:
    if not _SHARED_ENV.exists():
        return {}
    values: dict[str, str] = {}
    for line in _SHARED_ENV.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        values[k.strip()] = v.strip()
    return values


_SHARED = _load_shared_env()
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _SHARED.get("TEST_DATABASE_URL")
DATABASE_URL = os.environ.get("DATABASE_URL") or _SHARED.get("DATABASE_URL")


def _db_available() -> tuple[bool, str]:
    if not TEST_DATABASE_URL:
        return False, "TEST_DATABASE_URL not set (in env or shared/.env)"
    if not DATABASE_URL:
        return False, "DATABASE_URL not set (in env or shared/.env)"
    if TEST_DATABASE_URL == DATABASE_URL:
        return False, "TEST_DATABASE_URL must differ from DATABASE_URL"
    try:
        import psycopg
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute("select 1").fetchone()
    except Exception as e:
        return False, f"cannot connect to TEST_DATABASE_URL: {e}"
    return True, ""


DB_AVAILABLE, _DB_REASON = _db_available()

requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason=_DB_REASON)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    if not DB_AVAILABLE:
        pytest.skip(_DB_REASON)
    return TEST_DATABASE_URL  # type: ignore[return-value]
