"""Database engine, session factory, and base model."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# Convert sqlite:/// → sqlite+aiosqlite:///
DB_URL = settings.database_url
if DB_URL.startswith("sqlite:///"):
    DB_URL = "sqlite+aiosqlite:///" + DB_URL[len("sqlite:///") :]

engine = create_async_engine(
    DB_URL,
    echo=settings.log_level == "DEBUG",
    pool_pre_ping=True,
)

# Enable WAL, foreign keys, and normal sync on every new connection
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
