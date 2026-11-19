"""knowledge_base corpus, search indexes and indexations (S4)

Revision ID: 448949d0e134
Revises: 756db53032ab
Create Date: 2026-11-18 16:00:00

Corpus, générations d'index et faits de couverture
(docs/conception/bounded-contexts/knowledge-base.md §3.2, §3.3, §13 ; I8 à I10)
et clé étrangère composite des chunks vers le lot de leur unité (correction
reportée de la revue S3).

Verrous :

- trois tables neuves et vides : les clés étrangères vers
  ``document_versions`` prennent un verrou SHARE ROW EXCLUSIVE sur cette table
  le temps de l'instruction, sans parcours ;
- ``knowledge_units`` : la contrainte UNIQUE (id, version_id,
  chunking_policy_version) construit un index sous verrou ACCESS EXCLUSIVE
  (lectures et écritures bloquées pendant la construction, proportionnelle au
  nombre d'unités) ;
- ``document_chunks`` : la clé étrangère composite prend SHARE ROW EXCLUSIVE
  sur les deux tables et parcourt tous les chunks pour la valider.

Au volume de V1 (quelques milliers de lignes), l'ensemble dure quelques
millisecondes. Sur une grosse table, il faudrait ``CREATE UNIQUE INDEX
CONCURRENTLY`` puis ``ADD CONSTRAINT … USING INDEX``, et la clé étrangère en
``NOT VALID`` suivie de ``VALIDATE CONSTRAINT`` (verrou SHARE UPDATE
EXCLUSIVE, écritures non bloquées).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "448949d0e134"
down_revision: str | Sequence[str] | None = "756db53032ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.create_table(
        "knowledge_corpus",
        sa.Column("corpus_id", sa.Uuid(), nullable=False),
        sa.Column("active_index_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("corpus_id", name="pk_knowledge_corpus"),
        sa.CheckConstraint(
            "version >= 0", name="ck_knowledge_corpus_version_non_negative"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "search_indexes",
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("corpus_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("embedding_profile", postgresql.JSONB(), nullable=False),
        sa.Column("chunking_policy", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("index_id", name="pk_search_indexes"),
        sa.ForeignKeyConstraint(
            ["corpus_id"],
            [f"{SCHEMA}.knowledge_corpus.corpus_id"],
            name="fk_search_indexes_corpus_id_knowledge_corpus",
        ),
        sa.CheckConstraint(
            "status IN ('BUILDING', 'ACTIVE', 'RETIRED')",
            name="ck_search_indexes_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_search_indexes_one_active",
        "search_indexes",
        ["corpus_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "indexations",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("chunking_policy_version", sa.Text(), nullable=False),
        sa.Column("manifest_digest", sa.Text(), nullable=True),
        sa.Column("point_count", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("failure_retryable", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("version_id", "index_id", name="pk_indexations"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            [f"{SCHEMA}.document_versions.id"],
            name="fk_indexations_version_id_document_versions",
        ),
        sa.ForeignKeyConstraint(
            ["index_id"],
            [f"{SCHEMA}.search_indexes.index_id"],
            name="fk_indexations_index_id_search_indexes",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_indexations_status",
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (manifest_digest IS NOT NULL "
            "AND point_count IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_indexations_manifest_only_when_completed",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL "
            "AND failure_message IS NOT NULL AND failure_retryable IS NOT NULL)",
            name="ck_indexations_failure_only_when_failed",
        ),
        sa.CheckConstraint(
            "point_count IS NULL OR point_count >= 0",
            name="ck_indexations_point_count_non_negative",
        ),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_knowledge_units_id_batch",
        "knowledge_units",
        ["id", "version_id", "chunking_policy_version"],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_document_chunks_unit_batch",
        "document_chunks",
        "knowledge_units",
        ["unit_id", "version_id", "chunking_policy_version"],
        ["id", "version_id", "chunking_policy_version"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_document_chunks_unit_batch",
        "document_chunks",
        type_="foreignkey",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "uq_knowledge_units_id_batch",
        "knowledge_units",
        type_="unique",
        schema=SCHEMA,
    )
    op.drop_table("indexations", schema=SCHEMA)
    op.drop_index(
        "ix_search_indexes_one_active", table_name="search_indexes", schema=SCHEMA
    )
    op.drop_table("search_indexes", schema=SCHEMA)
    op.drop_table("knowledge_corpus", schema=SCHEMA)
