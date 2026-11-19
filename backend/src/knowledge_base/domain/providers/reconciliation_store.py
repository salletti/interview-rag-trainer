"""Port des constats SQL de la réconciliation (S8 ; knowledge-base.md §8, §9,
§10, O14, O15).

Lectures ensemblistes, chacune dans une session courte, hors de toute
transaction métier : elles décrivent ce qu'il faut peut-être corriger ; les
corrections relisent l'état dans leur propre transaction avant d'écrire. Une
seule écriture, sans agrégat : l'expiration des clés d'idempotence (O15).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    VersionStatus,
)
from knowledge_base.domain.providers.failed_work import (
    FailedWorkRepository,
    WorkItemRecord,
)
from knowledge_base.domain.providers.indexing_transaction import IndexingUnitOfWork
from knowledge_base.domain.services.reconciliation import DocumentFacts


@dataclass(frozen=True)
class PublicationCandidate:
    """Indexation ``COMPLETED`` d'une version ``EXTRACTED`` dans un index."""

    document_id: UUID
    version_id: UUID
    index_id: UUID
    document_status: DocumentStatus
    version_status: VersionStatus
    version_number: int
    published_number: int | None
    indexation_status: IndexationStatus
    index_status: IndexStatus


@dataclass(frozen=True)
class ExtractedVersion:
    """Version ``EXTRACTED`` : politiques de ses lots de chunks et index où
    elle a déjà une indexation (quel qu'en soit l'état)."""

    document_id: UUID
    version_id: UUID
    document_status: DocumentStatus
    version_status: VersionStatus
    policy_versions: frozenset[str]
    indexed_index_ids: frozenset[UUID]


@dataclass(frozen=True)
class IndexableVersionFacts:
    """Version ``PUBLISHED`` ou ``EXTRACTED`` d'un document actif (S9) :
    politiques de ses lots et index où elle a une indexation (quel qu'en soit
    l'état). Le document est actif par construction (prédicat de la lecture) :
    son statut n'est pas porté."""

    document_id: UUID
    version_id: UUID
    version_status: VersionStatus
    policy_versions: frozenset[str]
    indexed_index_ids: frozenset[UUID]


@dataclass(frozen=True)
class CompletedIndexation:
    """Indexation ``COMPLETED`` d'une version ``EXTRACTED`` ou ``PUBLISHED``
    d'un document actif, dans un index vivant : candidate à la revérification
    de son manifest."""

    document_id: UUID
    version_id: UUID
    index_id: UUID
    chunking_policy_version: str
    manifest_digest: str


@dataclass(frozen=True)
class FailedItemFacts:
    """Un item ``FAILED`` et l'état métier qu'il désigne (``None`` : absent
    ou payload illisible). ``live_work`` : un item du même travail (même
    ``kind``, même payload) est ``PENDING`` ou ``RUNNING``."""

    item: WorkItemRecord
    document_status: DocumentStatus | None
    purged: bool | None
    version_status: VersionStatus | None
    indexation_status: IndexationStatus | None
    live_work: bool


class ReconciliationStore(Protocol):
    async def documents(
        self, document_ids: Collection[UUID]
    ) -> Mapping[UUID, DocumentFacts]:
        """État des documents connus parmi ``document_ids`` (absents omis)."""
        ...

    async def documents_with_live_work(self, kind: str) -> frozenset[UUID]:
        """Documents qui ont un item ``kind`` ``PENDING`` ou ``RUNNING``."""
        ...

    async def publication_candidates(self) -> Sequence[PublicationCandidate]: ...

    async def extracted_versions(self) -> Sequence[ExtractedVersion]: ...

    async def indexable_versions(self) -> Sequence[IndexableVersionFacts]:
        """Versions ``PUBLISHED`` ou ``EXTRACTED`` des documents actifs (S9 :
        couverture d'un index ``BUILDING``)."""
        ...

    async def completed_indexations(self) -> Sequence[CompletedIndexation]:
        """Dans un ordre stable (version, index) : l'échantillonnage tire
        dedans."""
        ...

    async def failed_items(
        self, kinds: Collection[str]
    ) -> Sequence[FailedItemFacts]: ...

    async def delete_idempotency_keys_older_than(self, age: timedelta) -> int:
        """Clés enregistrées il y a strictement plus de ``age`` ; nombre de
        clés supprimées."""
        ...


class ReconciliationUnitOfWork(IndexingUnitOfWork, Protocol):
    """Transaction d'une correction : celle de l'indexation et de la
    publication (documents, corpus, indexations, work queue, chunks), plus les
    items ``FAILED`` (verrou, réouverture d'une purge)."""

    @property
    def failed_work(self) -> FailedWorkRepository: ...
