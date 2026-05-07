"""baseline_2026_05_07

Revision ID: 1ed07e31cab5
Revises:
Create Date: 2026-05-07 12:19:00.520623

Empty baseline. The schema this revision corresponds to was created
incrementally by the lifespan inline migrations in `app/main.py` over
2026-05-06 / 05-07; we stamp the database to this revision instead of
re-issuing those CREATEs. Future schema changes go in NEW revisions
under `alembic revision -m "<change>"` and run via `alembic upgrade head`.

The lifespan migrations are kept (they're idempotent) for backward
compatibility with installations that haven't been stamped yet.
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
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
