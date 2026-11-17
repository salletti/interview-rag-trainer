"""Tables du schéma PostgreSQL ``knowledge_base`` (S0 §2).

Ce ``MetaData`` est importé par ``alembic/env.py`` (``target_metadata``) ;
Alembic reste l'unique mécanisme de création du schéma.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA: Final = "knowledge_base"

WORK_STATUSES: Final = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")

metadata = MetaData(
    schema=SCHEMA,
    naming_convention={
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
    },
)

_statuses = ", ".join(f"'{status}'" for status in WORK_STATUSES)

work_items = Table(
    "work_items",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("dedup_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("5")),
    Column("fencing_token", BigInteger, nullable=False, server_default=text("0")),
    Column("lease_owner", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column(
        "available_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("dispatched_at", DateTime(timezone=True)),
    Column("last_error_code", Text),
    Column("last_error_message", Text),
    Column("correlation_id", Uuid),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("completed_at", DateTime(timezone=True)),
    UniqueConstraint("dedup_key"),
    CheckConstraint(f"status IN ({_statuses})", name="status"),
    CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
    CheckConstraint(
        "status <> 'RUNNING' OR lease_expires_at IS NOT NULL",
        name="running_has_lease",
    ),
)

Index(
    "ix_work_items_dispatchable",
    work_items.c.available_at,
    postgresql_where=work_items.c.status == "PENDING",
)
Index(
    "ix_work_items_running_lease",
    work_items.c.lease_expires_at,
    postgresql_where=work_items.c.status == "RUNNING",
)
Index("ix_work_items_kind_status", work_items.c.kind, work_items.c.status)
