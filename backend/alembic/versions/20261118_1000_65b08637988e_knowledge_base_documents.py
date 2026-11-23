"""knowledge_base documents, document_versions and idempotency_keys (S2)

Revision ID: 65b08637988e
Revises: f35542284e25
Create Date: 2026-11-18 10:00:00

Persistance de l'agrégat ``KnowledgeDocument`` et des clés d'idempotence de
l'import (docs/conception/bounded-contexts/knowledge-base.md §3.1, §4.1).
Trois tables neuves et vides : aucun verrou sur une table existante (la clé
étrangère ne référence que ``documents``, créée ici).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "65b08637988e"
down_revision: str | Sequence[str] | None = "f35542284e25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("topics", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("published_version_id", sa.Uuid(), nullable=True),
        sa.Column("visibility_revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'WITHDRAWN')", name="ck_documents_status"
        ),
        sa.CheckConstraint(
            "kind IN ('COURSE', 'QA_SHEET', 'REVISION_SHEET', 'TECHNICAL')",
            name="ck_documents_kind",
        ),
        sa.CheckConstraint(
            "visibility_revision >= 0",
            name="ck_documents_visibility_revision_non_negative",
        ),
        sa.CheckConstraint("version >= 0", name="ck_documents_version_non_negative"),
        schema=SCHEMA,
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("failure_retryable", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            [f"{SCHEMA}.documents.id"],
            name="fk_document_versions_document_id_documents",
        ),
        sa.UniqueConstraint("content_hash", name="uq_document_versions_content_hash"),
        sa.UniqueConstraint(
            "document_id", "number", name="uq_document_versions_document_id_number"
        ),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'EXTRACTED', 'FAILED', 'PUBLISHED', "
            "'DISCARDED', 'SUPERSEDED')",
            name="ck_document_versions_status",
        ),
        sa.CheckConstraint("number >= 1", name="ck_document_versions_number_positive"),
        sa.CheckConstraint(
            "size_bytes >= 0", name="ck_document_versions_size_bytes_non_negative"
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL "
            "AND failure_message IS NOT NULL AND failure_retryable IS NOT NULL)",
            name="ck_document_versions_failure_only_when_failed",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_document_versions_one_published",
        "document_versions",
        ["document_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("created", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("key", name="pk_idempotency_keys"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys", schema=SCHEMA)
    op.drop_table("document_versions", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
