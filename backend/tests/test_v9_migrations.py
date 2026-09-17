"""Guardrail for F25: a brand-new, empty database must be able to run
`alembic upgrade head` end to end, following the exact workflow documented
in the project README (``cd backend && alembic upgrade head``).

Before the fix, the baseline revision (1ed07e31cab5)'s upgrade() was a
no-op, so a fresh DB hit the *next* revision (e53bc1301436_add_llm_
classification), which calls ``inspector.get_columns("categorization_rules")``
unconditionally and blew up with NoSuchTableError — existing deployments
never noticed because the lifespan inline migrations in app/main.py bootstrap
the schema before Alembic ever runs.

Runs `alembic` as a real subprocess (matching how a user/deploy script
invokes it) against a throwaway sqlite file, so it never touches the
project's real data/finance.db.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_head() -> str:
    """Resolve the current head revision id without touching any DB."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    return script.get_current_head()


def _run_upgrade_head(db_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        # Absolute path -> 4-slash sqlite URL, matching Settings' own
        # construction (see backend/app/core/config.py db_path/resolved_
        # database_url) so env.py resolves it the same way the app would.
        "DATABASE_URL": f"sqlite:///{db_path}",
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fresh_empty_db_upgrade_head_bootstraps_schema(tmp_path):
    """Scenario 1: brand-new empty sqlite file -> `alembic upgrade head`
    must succeed and produce the full schema, stamped at head."""
    db_path = tmp_path / "fresh.db"

    result = _run_upgrade_head(db_path)
    assert result.returncode == 0, (
        f"alembic upgrade head failed on a fresh DB\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for expected in (
            "accounts",
            "transactions",
            "categories",
            "categorization_rules",
            # added by the next revision (e53bc1301436) — only reachable if
            # the baseline actually bootstrapped the tables it depends on.
            "categorization_notes",
            "app_settings",
        ):
            assert expected in tables, f"missing table {expected!r} after upgrade head"

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version is not None, "alembic_version table not stamped"
        assert version[0] == _alembic_head()
    finally:
        conn.close()


def test_upgrade_head_is_idempotent(tmp_path):
    """Scenario 2: running `alembic upgrade head` again against the same,
    already-migrated database must not error (and must not change state)."""
    db_path = tmp_path / "idempotent.db"

    first = _run_upgrade_head(db_path)
    assert first.returncode == 0, (
        f"first upgrade head failed\nstdout:\n{first.stdout}\nstderr:\n{first.stderr}"
    )

    conn = sqlite3.connect(db_path)
    try:
        version_after_first = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    finally:
        conn.close()

    second = _run_upgrade_head(db_path)
    assert second.returncode == 0, (
        f"second upgrade head failed\nstdout:\n{second.stdout}\nstderr:\n{second.stderr}"
    )

    conn = sqlite3.connect(db_path)
    try:
        version_after_second = conn.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    finally:
        conn.close()

    assert version_after_second == version_after_first == _alembic_head()
