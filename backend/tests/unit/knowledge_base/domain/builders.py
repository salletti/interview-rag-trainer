"""Fabriques de test du domaine Knowledge Base (S1) : pas de I/O, ids et
dates déterministes. Les identifiants sont fournis par l'appelant (le domaine
n'a pas de générateur), l'horloge est portée par ``DocumentSource.imported_at``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.domain.value_objects.failure_reason import FailureReason

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
DOCUMENT_ID = UUID(int=1)


def version_id(number: int) -> UUID:
    return UUID(int=1000 + number)


def content_hash(seed: int) -> ContentHash:
    return ContentHash(hashlib.sha256(str(seed).encode()).hexdigest())


def source(seed: int = 1) -> DocumentSource:
    return DocumentSource(
        filename=f"cours-{seed}.md",
        media_type="text/markdown",
        size_bytes=1000 + seed,
        content_hash=content_hash(seed),
        imported_at=T0 + timedelta(minutes=seed),
    )


def failure() -> FailureReason:
    return FailureReason(code="EXTRACTION_FAILED", message="boom", retryable=True)


def new_document(seed: int = 1) -> KnowledgeDocument:
    """Document ACTIVE avec la version 1 (RECEIVED, id ``version_id(1)``) ;
    les événements de création sont déjà vidés."""
    document = KnowledgeDocument.import_new(
        document_id=DOCUMENT_ID,
        version_id=version_id(1),
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=("python", "asyncio"),
        language="fr",
        source=source(seed),
    )
    document.pull_events()
    return document


def import_next(document: KnowledgeDocument, number: int) -> DocumentVersion:
    """Importe la version ``number`` (id ``version_id(number)``, source de
    graine ``number``)."""
    return document.import_version(version_id=version_id(number), source=source(number))


def extract(document: KnowledgeDocument, number: int) -> None:
    document.record_extraction(version_id(number))


def publish_numbered(document: KnowledgeDocument, number: int) -> None:
    extract(document, number)
    document.publish(version_id(number))


def restored_version(number: int, status: VersionStatus) -> DocumentVersion:
    return DocumentVersion(
        version_id=version_id(number),
        number=number,
        source=source(number),
        status=status,
        failure=failure() if status is VersionStatus.FAILED else None,
        created_at=source(number).imported_at,
    )


def restored_document(
    versions: Sequence[tuple[int, VersionStatus]],
    *,
    published: int | None = None,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    visibility_revision: int = 0,
) -> KnowledgeDocument:
    """Réhydratation (chemin de la persistance) d'un état que l'API publique
    n'atteint pas forcément : sert à tester les garde-fous indépendants."""
    return KnowledgeDocument.restore(
        document_id=DOCUMENT_ID,
        title="Python avancé",
        kind=DocumentKind.COURSE,
        topics=("python",),
        language="fr",
        status=status,
        published_version_id=None if published is None else version_id(published),
        visibility_revision=visibility_revision,
        version=7,
        versions=tuple(restored_version(n, s) for n, s in versions),
    )
