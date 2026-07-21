"""create initial application schema

Revision ID: 20260720_0001
Revises:
Create Date: 2026-07-20
"""
from typing import Sequence, Union

from alembic import op

from app.models import Base


revision: str = "20260720_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
