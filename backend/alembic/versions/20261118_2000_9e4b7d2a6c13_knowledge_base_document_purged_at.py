"""knowledge_base documents.purged_at (S7)

Revision ID: 9e4b7d2a6c13
Revises: c43c7e850745
Create Date: 2026-11-18 20:00:00

Purge d'un document retiré (S7 ; knowledge-base.md §6, §10 ; ADR 0012 point
6) : ``knowledge_base.documents.purged_at`` (``timestamptz``, nulle tant que
la purge n'est pas finalisée). Elle n'appartient pas à l'agrégat ; elle
dit si les lignes de versions du document (tombstones) retiennent encore
leurs blobs.

Verrous : ``ADD COLUMN`` d'une colonne nullable sans défaut ne réécrit pas la
table (PostgreSQL ≥ 11 : métadonnées seules) mais prend ACCESS EXCLUSIVE sur
``documents`` le temps de la modification du catalogue, soit quelques
millisecondes quelle que soit la taille de la table ; il attend toutefois la
fin des transactions qui lisent ``documents`` (une recherche, un import en
cours), et bloque les suivantes pendant cette attente. Aucun index : la
colonne n'est lue que par jointure depuis ``document_versions`` (empreinte
unique) et écrite par clé primaire.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9e4b7d2a6c13"
down_revision: str | Sequence[str] | None = "c43c7e850745"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("documents", "purged_at", schema=SCHEMA)
