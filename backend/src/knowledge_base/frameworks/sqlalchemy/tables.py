"""Tables du schéma PostgreSQL ``knowledge_base`` : ``work_items`` (S0 §2),
``documents``, ``document_versions`` et ``idempotency_keys`` (S2),
``knowledge_units`` et ``document_chunks`` (S3).

Ce ``MetaData`` est importé par ``alembic/env.py`` (``target_metadata``) ;
Alembic reste l'unique mécanisme de création du schéma.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

SCHEMA: Final = "knowledge_base"

WORK_STATUSES: Final = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
# Miroirs des énumérations du domaine (S1) ; une valeur ajoutée au domaine
# demande une migration.
DOCUMENT_STATUSES: Final = ("ACTIVE", "WITHDRAWN")
DOCUMENT_KINDS: Final = ("COURSE", "QA_SHEET", "REVISION_SHEET", "TECHNICAL")
UNIT_TYPES: Final = ("QA", "SECTION", "DEFINITION")
VERSION_STATUSES: Final = (
    "RECEIVED",
    "EXTRACTED",
    "FAILED",
    "PUBLISHED",
    "DISCARDED",
    "SUPERSEDED",
)

metadata = MetaData(
    schema=SCHEMA,
    naming_convention={
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    },
)


def _one_of(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_statuses = _one_of(WORK_STATUSES)

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

documents = Table(
    "documents",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("title", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("topics", ARRAY(Text), nullable=False),
    Column("language", Text, nullable=False),
    Column("status", Text, nullable=False),
    # Pas de clé étrangère vers document_versions (référence circulaire) : I4
    # est porté par l'index unique partiel de document_versions.
    Column("published_version_id", Uuid),
    Column("visibility_revision", Integer, nullable=False),
    # Verrou optimiste (I2) : UPDATE ... WHERE version = :lue.
    Column("version", Integer, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(f"status IN ({_one_of(DOCUMENT_STATUSES)})", name="status"),
    CheckConstraint(f"kind IN ({_one_of(DOCUMENT_KINDS)})", name="kind"),
    CheckConstraint(
        "visibility_revision >= 0", name="visibility_revision_non_negative"
    ),
    CheckConstraint("version >= 0", name="version_non_negative"),
)

document_versions = Table(
    "document_versions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("document_id", Uuid, ForeignKey(documents.c.id), nullable=False),
    Column("number", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("filename", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False),
    Column("failure_code", Text),
    Column("failure_message", Text),
    Column("failure_retryable", Boolean),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Global, pas seulement par document : I3 et la décision O2 (un contenu
    # n'appartient qu'à un document). Sert aussi d'index à find_by_content_hash.
    UniqueConstraint("content_hash"),
    UniqueConstraint(
        "document_id", "number", name="uq_document_versions_document_id_number"
    ),
    CheckConstraint(f"status IN ({_one_of(VERSION_STATUSES)})", name="status"),
    CheckConstraint("number >= 1", name="number_positive"),
    CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
    # failure n'est renseigné qu'à l'état FAILED (S1).
    CheckConstraint(
        "(status = 'FAILED') = (failure_code IS NOT NULL "
        "AND failure_message IS NOT NULL AND failure_retryable IS NOT NULL)",
        name="failure_only_when_failed",
    ),
)

# I4 : au plus une version PUBLISHED par document.
Index(
    "ix_document_versions_one_published",
    document_versions.c.document_id,
    unique=True,
    postgresql_where=document_versions.c.status == "PUBLISHED",
)

# Sans clé étrangère : un enregistrement rend un résultat passé, il ne
# garantit pas l'existence actuelle du document.
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("key", Text, primary_key=True),
    Column("fingerprint", Text, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("number", Integer, nullable=False),
    Column("created", Boolean, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)

# Unités et chunks (S3, knowledge-base.md §3.4) : données dérivées immuables,
# écrites par lot dans la transaction qui passe la version à EXTRACTED (I7).
# Pas d'updated_at : rien n'est jamais modifié. Identité d'un lot :
# (version_id, chunking_policy_version) ; identifiants déterministes (UUIDv5).
_PAGES_CHECK: Final = (
    "(page_start IS NULL) = (page_end IS NULL) "
    "AND (page_start IS NULL OR (page_start >= 1 AND page_start <= page_end))"
)

knowledge_units = Table(
    "knowledge_units",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("version_id", Uuid, ForeignKey(document_versions.c.id), nullable=False),
    Column("chunking_policy_version", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("unit_type", Text, nullable=False),
    Column("question_text", Text),
    Column("section_path", ARRAY(Text), nullable=False),
    Column("page_start", Integer),
    Column("page_end", Integer),
    Column("token_count", Integer, nullable=False),
    # Blocs rendus de l'unité : la reconstruction small-to-big (S5) les relit.
    Column("text", Text, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    # Sert aussi d'index aux lectures par version.
    UniqueConstraint(
        "version_id",
        "chunking_policy_version",
        "ordinal",
        name="uq_knowledge_units_batch_ordinal",
    ),
    CheckConstraint(f"unit_type IN ({_one_of(UNIT_TYPES)})", name="unit_type"),
    CheckConstraint(
        "(unit_type = 'QA') = (question_text IS NOT NULL)", name="question_only_for_qa"
    ),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    CheckConstraint("token_count >= 0", name="token_count_non_negative"),
    CheckConstraint(_PAGES_CHECK, name="pages"),
)

document_chunks = Table(
    "document_chunks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("unit_id", Uuid, ForeignKey(knowledge_units.c.id), nullable=False),
    # Dénormalisé depuis l'unité : lectures et purges par version (S4, S7).
    Column("version_id", Uuid, ForeignKey(document_versions.c.id), nullable=False),
    Column("chunking_policy_version", Text, nullable=False),
    Column("ordinal_in_unit", Integer, nullable=False),
    Column("contextual_text", Text, nullable=False),
    Column("chunk_hash", Text, nullable=False),
    Column("token_count", Integer, nullable=False),
    Column("citation", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    UniqueConstraint(
        "unit_id", "ordinal_in_unit", name="uq_document_chunks_unit_ordinal"
    ),
    CheckConstraint("ordinal_in_unit >= 0", name="ordinal_in_unit_non_negative"),
    CheckConstraint("token_count >= 0", name="token_count_non_negative"),
)

Index(
    "ix_document_chunks_version_policy",
    document_chunks.c.version_id,
    document_chunks.c.chunking_policy_version,
)
