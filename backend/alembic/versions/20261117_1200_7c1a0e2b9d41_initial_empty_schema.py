"""initial empty schema

Revision ID: 7c1a0e2b9d41
Revises:
Create Date: 2026-11-17 12:00:00

Migration volontairement vide : elle vérifie que l'outillage Alembic
fonctionne (table ``alembic_version`` créée) sans figer la forme d'un schéma
métier avant qu'un vertical slice ne l'exige.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "7c1a0e2b9d41"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
