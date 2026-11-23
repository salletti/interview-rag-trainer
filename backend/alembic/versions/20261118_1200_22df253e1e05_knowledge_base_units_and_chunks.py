"""knowledge_base knowledge_units and document_chunks (S3)

Revision ID: 22df253e1e05
Revises: 65b08637988e
Create Date: 2026-11-18 12:00:00

Unités de connaissance et chunks dérivés d'une version
(docs/conception/bounded-contexts/knowledge-base.md §3.4, I7, I13). Deux
tables neuves et vides. Les clés étrangères vers ``document_versions``
prennent un verrou SHARE ROW EXCLUSIVE sur cette table le temps de la
création (aucun parcours : les tables créées sont vides) ; il bloque les
écritures concurrentes sur les versions pendant quelques millisecondes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "22df253e1e05"
down_revision: str | Sequence[str] | None = "65b08637988e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"
PAGES_CHECK = (
    "(page_start IS NULL) = (page_end IS NULL) "
    "AND (page_start IS NULL OR (page_start >= 1 AND page_start <= page_end))"
)


def upgrade() -> None:
    op.create_table(
        "knowledge_units",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("chunking_policy_version", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("unit_type", sa.Text(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=True),
        sa.Column("section_path", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_units"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            [f"{SCHEMA}.document_versions.id"],
            name="fk_knowledge_units_version_id_document_versions",
        ),
        sa.UniqueConstraint(
            "version_id",
            "chunking_policy_version",
            "ordinal",
            name="uq_knowledge_units_batch_ordinal",
        ),
        sa.CheckConstraint(
            "unit_type IN ('QA', 'SECTION', 'DEFINITION')",
            name="ck_knowledge_units_unit_type",
        ),
        sa.CheckConstraint(
            "(unit_type = 'QA') = (question_text IS NOT NULL)",
            name="ck_knowledge_units_question_only_for_qa",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_knowledge_units_ordinal_non_negative"
        ),
        sa.CheckConstraint(
            "token_count >= 0", name="ck_knowledge_units_token_count_non_negative"
        ),
        sa.CheckConstraint(PAGES_CHECK, name="ck_knowledge_units_pages"),
        schema=SCHEMA,
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("chunking_policy_version", sa.Text(), nullable=False),
        sa.Column("ordinal_in_unit", sa.Integer(), nullable=False),
        sa.Column("contextual_text", sa.Text(), nullable=False),
        sa.Column("chunk_hash", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("citation", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            [f"{SCHEMA}.knowledge_units.id"],
            name="fk_document_chunks_unit_id_knowledge_units",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            [f"{SCHEMA}.document_versions.id"],
            name="fk_document_chunks_version_id_document_versions",
        ),
        sa.UniqueConstraint(
            "unit_id", "ordinal_in_unit", name="uq_document_chunks_unit_ordinal"
        ),
        sa.CheckConstraint(
            "ordinal_in_unit >= 0",
            name="ck_document_chunks_ordinal_in_unit_non_negative",
        ),
        sa.CheckConstraint(
            "token_count >= 0", name="ck_document_chunks_token_count_non_negative"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_document_chunks_version_policy",
        "document_chunks",
        ["version_id", "chunking_policy_version"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("document_chunks", schema=SCHEMA)
    op.drop_table("knowledge_units", schema=SCHEMA)
