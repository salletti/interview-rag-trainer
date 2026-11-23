"""knowledge_base schema and work_items table (S0)

Revision ID: f35542284e25
Revises: 7c1a0e2b9d41
Create Date: 2026-11-17 23:30:00

Crée le schéma ``knowledge_base`` et la table ``work_items`` de la work queue
durable (docs/conception/bounded-contexts/knowledge-base-s0.md §2). Table
neuve et vide : aucun verrou prolongé sur une table existante.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f35542284e25"
down_revision: str | Sequence[str] | None = "7c1a0e2b9d41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge_base"


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema(SCHEMA, if_not_exists=True))
    op.create_table(
        "work_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_work_items"),
        sa.UniqueConstraint("dedup_key", name="uq_work_items_dedup_key"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_work_items_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_work_items_attempts_non_negative"),
        sa.CheckConstraint(
            "max_attempts >= 1", name="ck_work_items_max_attempts_positive"
        ),
        sa.CheckConstraint(
            "status <> 'RUNNING' OR lease_expires_at IS NOT NULL",
            name="ck_work_items_running_has_lease",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_work_items_dispatchable",
        "work_items",
        ["available_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_work_items_running_lease",
        "work_items",
        ["lease_expires_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index(
        "ix_work_items_kind_status", "work_items", ["kind", "status"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("work_items", schema=SCHEMA)
    # Sans CASCADE : échoue si un objet inconnu de cette migration y a été créé.
    op.execute(sa.schema.DropSchema(SCHEMA))
