"""Alembic env — async-aware, reads URL from app.core.config.

Wired in 2026-05-07 to give us a versioned schema-change trail. Existing
lifespan migrations in `app/main.py` are left alone (they're idempotent
and already applied to the user's DB); new schema changes from now on
should land here as a new revision.

Usage:
    cd backend
    alembic revision --autogenerate -m "add foo column"
    alembic upgrade head
    alembic history
    alembic stamp head   # mark current DB as up-to-date with current head
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make `app` importable when running `alembic` from backend/
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app import models  # noqa: F401, E402  (registers ORM tables on Base.metadata)

config = context.config

# Inject the live DB URL (async aiosqlite driver — alembic uses async engine
# below). `resolved_database_url` is `sqlite:///...`; we promote it to async.
_settings = get_settings()
_url = _settings.resolved_database_url
if _url.startswith("sqlite:///"):
    _url = "sqlite+aiosqlite:///" + _url[len("sqlite:///"):]
config.set_main_option("sqlalchemy.url", _url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite — most ALTERs need batch mode
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
