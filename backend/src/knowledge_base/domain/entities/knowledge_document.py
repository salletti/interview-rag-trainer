"""Agrégat ``KnowledgeDocument`` et entité interne ``DocumentVersion``
(knowledge-base.md §3.1, §4.1 I1 à I6, §5.1 et 5.2, §8, §10).

Le domaine ne génère ni identifiant ni date : l'appelant fournit les UUID et
l'horloge passe par ``DocumentSource.imported_at``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final, Self
from uuid import UUID

from knowledge_base.domain.events.document_events import (
    DocumentEvent,
    DocumentImported,
    DocumentVersionDiscarded,
    DocumentVersionExtracted,
    DocumentVersionPublished,
    DocumentWithdrawn,
)
from knowledge_base.domain.exceptions.document_errors import (
    DocumentWithdrawnError,
    DuplicateContentError,
    InvalidVersionTransitionError,
    StalePublicationError,
    VersionNotFoundError,
)
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.document_source import DocumentSource
from knowledge_base.domain.value_objects.failure_reason import FailureReason


class DocumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


class VersionStatus(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    FAILED = "FAILED"
    PUBLISHED = "PUBLISHED"
    DISCARDED = "DISCARDED"
    SUPERSEDED = "SUPERSEDED"


# Diagramme de §5.2, seule source des transitions autorisées. Les versions qui
# peuvent encore devenir DISCARDED sont exactement celles qu'I6 écarte.
_TRANSITIONS: Final[Mapping[VersionStatus, frozenset[VersionStatus]]] = {
    VersionStatus.RECEIVED: frozenset(
        {VersionStatus.EXTRACTED, VersionStatus.FAILED, VersionStatus.DISCARDED}
    ),
    VersionStatus.EXTRACTED: frozenset(
        {VersionStatus.PUBLISHED, VersionStatus.DISCARDED}
    ),
    VersionStatus.FAILED: frozenset({VersionStatus.RECEIVED, VersionStatus.DISCARDED}),
    VersionStatus.PUBLISHED: frozenset({VersionStatus.SUPERSEDED}),
    VersionStatus.DISCARDED: frozenset(),
    VersionStatus.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True)
class DocumentVersion:
    """Contenu importé à un instant (I1) : seul l'agrégat en produit un nouvel
    état, en remplaçant l'objet."""

    version_id: UUID
    number: int
    source: DocumentSource
    status: VersionStatus
    failure: FailureReason | None
    created_at: datetime


class KnowledgeDocument:
    """Aggregate Root. Toute opération refusée lève une
    ``KnowledgeDocumentError`` sans aucun effet (ni état, ni événement)."""

    def __init__(
        self,
        *,
        document_id: UUID,
        title: str,
        kind: DocumentKind,
        topics: tuple[str, ...],
        language: str,
        status: DocumentStatus,
        published_version_id: UUID | None,
        visibility_revision: int,
        version: int,
        versions: Iterable[DocumentVersion],
    ) -> None:
        self._document_id = document_id
        self._title = title
        self._kind = kind
        self._topics = topics
        self._language = language
        self._status = status
        self._published_version_id = published_version_id
        self._visibility_revision = visibility_revision
        self._version = version
        self._versions = sorted(versions, key=lambda v: v.number)
        self._events: list[DocumentEvent] = []

    @classmethod
    def import_new(
        cls,
        *,
        document_id: UUID,
        version_id: UUID,
        title: str,
        kind: DocumentKind,
        topics: tuple[str, ...],
        language: str,
        source: DocumentSource,
    ) -> Self:
        if not title.strip():
            raise ValueError("title ne peut pas être vide")
        document = cls(
            document_id=document_id,
            title=title,
            kind=kind,
            topics=topics,
            language=language,
            status=DocumentStatus.ACTIVE,
            published_version_id=None,
            visibility_revision=0,
            version=0,
            versions=(),
        )
        document._append_version(version_id, source)
        return document

    @classmethod
    def restore(
        cls,
        *,
        document_id: UUID,
        title: str,
        kind: DocumentKind,
        topics: tuple[str, ...],
        language: str,
        status: DocumentStatus,
        published_version_id: UUID | None,
        visibility_revision: int,
        version: int,
        versions: Iterable[DocumentVersion],
    ) -> Self:
        """Réhydratation par la persistance : l'état est repris tel quel, sans
        événement ni contrôle de transition."""
        return cls(
            document_id=document_id,
            title=title,
            kind=kind,
            topics=topics,
            language=language,
            status=status,
            published_version_id=published_version_id,
            visibility_revision=visibility_revision,
            version=version,
            versions=versions,
        )

    # --- lecture ----------------------------------------------------------------

    @property
    def document_id(self) -> UUID:
        return self._document_id

    @property
    def title(self) -> str:
        return self._title

    @property
    def kind(self) -> DocumentKind:
        return self._kind

    @property
    def topics(self) -> tuple[str, ...]:
        return self._topics

    @property
    def language(self) -> str:
        return self._language

    @property
    def status(self) -> DocumentStatus:
        return self._status

    @property
    def published_version_id(self) -> UUID | None:
        return self._published_version_id

    @property
    def visibility_revision(self) -> int:
        return self._visibility_revision

    @property
    def version(self) -> int:
        """Verrou optimiste : incrémenté par la persistance, jamais ici."""
        return self._version

    @property
    def versions(self) -> tuple[DocumentVersion, ...]:
        return tuple(self._versions)

    def get_version(self, version_id: UUID) -> DocumentVersion:
        return self._versions[self._index_of(version_id)]

    def pull_events(self) -> list[DocumentEvent]:
        events, self._events = self._events, []
        return events

    # --- opérations ---------------------------------------------------------------

    def import_version(
        self, *, version_id: UUID, source: DocumentSource
    ) -> DocumentVersion:
        """Ajoute la version suivante (I2, I3) et écarte les versions
        antérieures non publiées (I6)."""
        self._ensure_active("import d'une version")
        for existing in self._versions:
            if existing.source.content_hash == source.content_hash:
                raise DuplicateContentError(
                    self._document_id, source.content_hash, existing.version_id
                )
        older = list(self._versions)
        imported = self._append_version(version_id, source)
        for previous in older:
            if VersionStatus.DISCARDED in _TRANSITIONS[previous.status]:
                self._move(previous.version_id, VersionStatus.DISCARDED)
                self._events.append(
                    DocumentVersionDiscarded(
                        document_id=self._document_id,
                        version_id=previous.version_id,
                        number=previous.number,
                        replaced_by_version_id=version_id,
                    )
                )
        return imported

    def record_extraction(self, version_id: UUID) -> None:
        self._move(version_id, VersionStatus.EXTRACTED)
        self._events.append(
            DocumentVersionExtracted(
                document_id=self._document_id, version_id=version_id
            )
        )

    def fail_extraction(self, version_id: UUID, reason: FailureReason) -> None:
        self._move(version_id, VersionStatus.FAILED, reason)

    def retry_extraction(self, version_id: UUID) -> None:
        self._move(version_id, VersionStatus.RECEIVED)

    def publish(self, version_id: UUID) -> None:
        """Rend la version visible (I4, I5) ; la précédente devient
        ``SUPERSEDED``. Les contrôles précèdent toute modification."""
        self._ensure_active("publication")
        candidate = self.get_version(version_id)
        self._ensure_transition(candidate, VersionStatus.PUBLISHED)
        superseded_id = self._published_version_id
        if superseded_id is not None:
            published = self.get_version(superseded_id)
            if candidate.number <= published.number:
                raise StalePublicationError(
                    self._document_id, candidate.number, published.number
                )
            self._move(superseded_id, VersionStatus.SUPERSEDED)
        self._move(version_id, VersionStatus.PUBLISHED)
        self._published_version_id = version_id
        self._visibility_revision += 1
        self._events.append(
            DocumentVersionPublished(
                document_id=self._document_id,
                version_id=version_id,
                number=candidate.number,
                visibility_revision=self._visibility_revision,
                superseded_version_id=superseded_id,
            )
        )

    def withdraw(self) -> None:
        """Retrait logique : l'état des versions est inchangé, la visibilité
        est portée par le statut du document (§5.2)."""
        self._ensure_active("retrait")
        self._status = DocumentStatus.WITHDRAWN
        self._visibility_revision += 1
        self._events.append(
            DocumentWithdrawn(
                document_id=self._document_id,
                visibility_revision=self._visibility_revision,
            )
        )

    # --- interne ------------------------------------------------------------------

    def _append_version(
        self, version_id: UUID, source: DocumentSource
    ) -> DocumentVersion:
        number = max((v.number for v in self._versions), default=0) + 1
        version = DocumentVersion(
            version_id=version_id,
            number=number,
            source=source,
            status=VersionStatus.RECEIVED,
            failure=None,
            created_at=source.imported_at,
        )
        self._versions.append(version)
        self._events.append(
            DocumentImported(
                document_id=self._document_id,
                version_id=version_id,
                number=number,
                content_hash=source.content_hash,
            )
        )
        return version

    def _move(
        self,
        version_id: UUID,
        target: VersionStatus,
        failure: FailureReason | None = None,
    ) -> None:
        """``failure`` n'est renseigné qu'à l'état ``FAILED`` : toute autre
        transition l'efface."""
        index = self._index_of(version_id)
        current = self._versions[index]
        self._ensure_transition(current, target)
        self._versions[index] = replace(current, status=target, failure=failure)

    def _ensure_transition(
        self, version: DocumentVersion, target: VersionStatus
    ) -> None:
        if target not in _TRANSITIONS[version.status]:
            raise InvalidVersionTransitionError(
                self._document_id, version.version_id, version.status, target
            )

    def _ensure_active(self, operation: str) -> None:
        if self._status is DocumentStatus.WITHDRAWN:
            raise DocumentWithdrawnError(self._document_id, operation)

    def _index_of(self, version_id: UUID) -> int:
        for index, version in enumerate(self._versions):
            if version.version_id == version_id:
                return index
        raise VersionNotFoundError(self._document_id, version_id)
