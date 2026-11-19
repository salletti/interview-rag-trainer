"""knowledge_base work_items.reopen_count (S8)

Revision ID: 5d2f8c1a7b30
Revises: 9e4b7d2a6c13
Create Date: 2026-11-18 22:00:00

Réouverture d'un item ``FAILED`` (S8 ; S0 §3, transition ``reopen``) :
``knowledge_base.work_items.reopen_count`` (``integer NOT NULL DEFAULT 0``,
``CHECK >= 0``) compte les réouvertures, par ``RetryIngestion`` ou par la
réconciliation, qui ne rouvre plus une purge en échec au-delà de
``KB_RECONCILE_MAX_PURGE_REOPENINGS`` (knowledge-base.md §16, précision S8).

Verrous : ``ADD COLUMN`` avec un défaut constant ne réécrit pas la table
(PostgreSQL ≥ 11 : défaut stocké dans le catalogue) ; il prend ACCESS
EXCLUSIVE sur ``work_items`` le temps de la modification du catalogue. Le
``CHECK`` est validé à l'ajout par un parcours complet de la table, sous le
même verrou : linéaire en nombre d'items (quelques milliers en V1, aucune
purge des items terminés), donc de l'ordre de la milliseconde à la dizaine de
millisecondes ; pendant ce temps, claims, dispatch et heartbeats attendent.
Sur une très grosse table, il faudrait l'ajouter ``NOT VALID`` puis le
valider à part (``VALIDATE CONSTRAINT`` ne prend que SHARE UPDATE EXCLUSIVE).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5d2f8c1a7b30"
down_revision: str | Sequence[str] | None = "9e4b7d2a6c13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.add_column(
        "work_items",
        sa.Column(
            "reopen_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_work_items_reopen_count_non_negative",
        "work_items",
        "reopen_count >= 0",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_work_items_reopen_count_non_negative",
        "work_items",
        type_="check",
        schema=SCHEMA,
    )
    op.drop_column("work_items", "reopen_count", schema=SCHEMA)
