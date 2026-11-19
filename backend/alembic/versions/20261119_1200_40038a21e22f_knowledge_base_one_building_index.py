"""knowledge_base search_indexes : au plus un index BUILDING par corpus (revue S9)

Revision ID: 40038a21e22f
Revises: 5d2f8c1a7b30
Create Date: 2026-11-19 12:00:00

Une seule migration blue/green en cours par corpus (knowledge-base.md §3.2,
§5.4) : invariant de ``KnowledgeCorpus.provision_index``, doublé ici, comme
I10 (``ix_search_indexes_one_active``), d'un index unique partiel
``(corpus_id) WHERE status = 'BUILDING'``.

Verrous : ``CREATE UNIQUE INDEX`` (sans ``CONCURRENTLY``) prend SHARE sur
``search_indexes`` le temps de construire l'index : les lectures passent, les
écritures (provisionnement, activation, abandon) attendent. La table compte une
ligne par génération d'index (quelques unités en V1) : la construction est de
l'ordre de la milliseconde. La migration échoue si deux index d'un même corpus
sont déjà ``BUILDING`` (aucun chemin de production ne le permettait depuis S9 :
``StartIndexMigration`` refusait déjà un second index en construction) ; il
faudrait alors en abandonner un avant de migrer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "40038a21e22f"
down_revision: str | Sequence[str] | None = "5d2f8c1a7b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.create_index(
        "ix_search_indexes_one_building",
        "search_indexes",
        ["corpus_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'BUILDING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_search_indexes_one_building", table_name="search_indexes", schema=SCHEMA
    )
