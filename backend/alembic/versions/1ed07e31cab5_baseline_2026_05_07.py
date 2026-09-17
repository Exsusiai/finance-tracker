"""baseline_2026_05_07

Revision ID: 1ed07e31cab5
Revises:
Create Date: 2026-05-07 12:19:00.520623

The schema this revision corresponds to was created incrementally by the
lifespan inline migrations in `app/main.py` over 2026-05-06 / 05-07; we
stamp existing databases to this revision instead of re-issuing those
CREATEs. Future schema changes go in NEW revisions under
`alembic revision -m "<change>"` and run via `alembic upgrade head`.

The lifespan migrations are kept (they're idempotent) for backward
compatibility with installations that haven't been stamped yet.

`upgrade()` has two meanings depending on what it runs against:
  - Existing DB (already has an `accounts` table, e.g. via the lifespan
    inline migrations, or `alembic stamp head`): no-op, as before — the
    schema is already there.
  - Brand-new empty DB: full bootstrap via `Base.metadata.create_all()`.
    Without this, `alembic upgrade head` on a fresh database fails at the
    *next* revision (`e53bc1301436_add_llm_classification`), which calls
    `inspector.get_columns("categorization_rules")` unconditionally and
    assumes the baseline tables already exist (F25).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1ed07e31cab5'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("accounts"):
        from app.db import Base
        from app import models  # noqa: F401  (registers ORM tables on Base.metadata)

        Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    """Downgrade schema."""
    pass
