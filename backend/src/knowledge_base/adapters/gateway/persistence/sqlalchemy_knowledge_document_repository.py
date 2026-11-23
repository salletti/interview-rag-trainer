"""Dépôt SQLAlchemy de ``KnowledgeDocument`` : mapping explicite entre les
lignes ``documents`` / ``document_versions`` et l'agrégat (aucun objet ORM).

Verrou optimiste (I2) : ``save`` écrit le document **avant** ses versions,
par ``UPDATE ... WHERE version = <version lue>`` ; zéro ligne touchée lève
``ConcurrentModificationError``, sans aucune ligne de version écrite.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.gateway.persistence.constraint_violations import (
    violated_constraint,
)
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    DocumentVersion,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
    ContentHashTakenError,
    ContentOwner,
)
from knowledge_base.domain.value_objects.content_hash import ContentHash
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.frameworks.sqlalchemy.tables import document_versions, documents

CONTENT_HASH_UNIQUE: Final = "uq_document_versions_content_hash"

_d = documents.c
_v = document_versions.c


@dataclass(frozen=True)
class _Persisted:
    """Ce que la base contient pour un document, vu de cette transaction."""

    lock_version: int
    versions: Mapping[UUID, DocumentVersion]


class SqlAlchemyKnowledgeDocumentRepository:
    """Travaille dans la session de l'Unit of Work ; ne valide ni n'annule
    jamais la transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._persisted: dict[UUID, _Persisted] = {}

    async def get(self, document_id: UUID) -> KnowledgeDocument | None:
        row = (
            await self._session.execute(select(documents).where(_d.id == document_id))
        ).one_or_none()
        if row is None:
            return None
        version_rows = (
            await self._session.execute(
                select(document_versions)
                .where(_v.document_id == document_id)
                .order_by(_v.number)
            )
        ).all()
        document = _to_document(row, version_rows)
        # La première lecture fait foi : la relecture d'un document déjà lu
        # dans cette transaction ne doit pas avancer la version du verrou.
        if document.document_id not in self._persisted:
            self._remember(document, document.version)
        return document

    async def find_by_content_hash(
        self, content_hash: ContentHash
    ) -> ContentOwner | None:
        row = (
            await self._session.execute(
                select(_v.document_id, _d.status, _v.id, _v.number)
                .join(documents, _d.id == _v.document_id)
                .where(_v.content_hash == content_hash.value)
            )
        ).one_or_none()
        if row is None:
            return None
        return ContentOwner(
            document_id=row.document_id,
            document_status=DocumentStatus(row.status),
            version_id=row.id,
            version_number=row.number,
        )

    async def add(self, document: KnowledgeDocument) -> None:
        await self._session.execute(
            insert(documents).values(
                id=document.document_id,
                version=document.version,
                **_document_values(document),
            )
        )
        await self._insert_versions(document.document_id, document.versions)
        self._remember(document, document.version)

    async def save(self, document: KnowledgeDocument) -> None:
        persisted = self._persisted.get(document.document_id)
        if persisted is None:
            raise ValueError(
                f"document {document.document_id} enregistré sans avoir été lu ni "
                "ajouté dans cette Unit of Work : version du verrou inconnue"
            )
        lock_version = await self._session.scalar(
            update(documents)
            .where(
                _d.id == document.document_id,
                _d.version == persisted.lock_version,
            )
            .values(
                version=_d.version + 1,
                updated_at=func.now(),
                **_document_values(document),
            )
            .returning(_d.version)
        )
        if lock_version is None:
            raise ConcurrentModificationError(document.document_id)
        changed = [
            v
            for v in document.versions
            if v.version_id in persisted.versions
            and persisted.versions[v.version_id] != v
        ]
        # Une version qui quitte PUBLISHED d'abord : l'index unique partiel
        # (I4) n'est pas différable.
        changed.sort(
            key=lambda v: (
                persisted.versions[v.version_id].status is not VersionStatus.PUBLISHED
            )
        )
        for version in changed:
            await self._session.execute(
                update(document_versions)
                .where(_v.id == version.version_id)
                .values(**_mutable_version_values(version))
            )
        await self._insert_versions(
            document.document_id,
            [v for v in document.versions if v.version_id not in persisted.versions],
        )
        self._remember(document, lock_version)

    async def _insert_versions(
        self, document_id: UUID, versions: Sequence[DocumentVersion]
    ) -> None:
        if not versions:
            return
        try:
            await self._session.execute(
                insert(document_versions),
                [_version_values(document_id, v) for v in versions],
            )
        except IntegrityError as error:
            if violated_constraint(error) == CONTENT_HASH_UNIQUE:
                raise ContentHashTakenError(document_id) from error
            raise

    def _remember(self, document: KnowledgeDocument, lock_version: int) -> None:
        self._persisted[document.document_id] = _Persisted(
            lock_version, {v.version_id: v for v in document.versions}
        )


def _document_values(document: KnowledgeDocument) -> dict[str, Any]:
    return {
        "title": document.title,
        "kind": document.kind.value,
        "topics": list(document.topics),
        "language": document.language,
        "status": document.status.value,
        "published_version_id": document.published_version_id,
        "visibility_revision": document.visibility_revision,
    }


def _mutable_version_values(version: DocumentVersion) -> dict[str, Any]:
    """Seuls l'état et la cause d'échec changent (I1 : source immuable)."""
    failure = version.failure
    return {
        "status": version.status.value,
        "failure_code": None if failure is None else failure.code,
        "failure_message": None if failure is None else failure.message,
        "failure_retryable": None if failure is None else failure.retryable,
    }


def _version_values(document_id: UUID, version: DocumentVersion) -> dict[str, Any]:
    source = version.source
    return {
        "id": version.version_id,
        "document_id": document_id,
        "number": version.number,
        "filename": source.filename,
        "media_type": source.media_type,
        "size_bytes": source.size_bytes,
        "content_hash": source.content_hash.value,
        "imported_at": source.imported_at,
        "created_at": version.created_at,
        **_mutable_version_values(version),
    }


def _to_document(row: Row[Any], version_rows: Iterable[Row[Any]]) -> KnowledgeDocument:
    return KnowledgeDocument.restore(
        document_id=row.id,
        title=row.title,
        kind=DocumentKind(row.kind),
        topics=tuple(row.topics),
        language=row.language,
        status=DocumentStatus(row.status),
        published_version_id=row.published_version_id,
        visibility_revision=row.visibility_revision,
        version=row.version,
        versions=[_to_version(v) for v in version_rows],
    )


def _to_version(row: Row[Any]) -> DocumentVersion:
    failure = None
    if row.failure_code is not None:
        failure = FailureReason(
            code=row.failure_code,
            message=row.failure_message,
            retryable=row.failure_retryable,
        )
    return DocumentVersion(
        version_id=row.id,
        number=row.number,
        source=DocumentSource(
            filename=row.filename,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            content_hash=ContentHash(row.content_hash),
            imported_at=row.imported_at,
        ),
        status=VersionStatus(row.status),
        failure=failure,
        created_at=row.created_at,
    )
