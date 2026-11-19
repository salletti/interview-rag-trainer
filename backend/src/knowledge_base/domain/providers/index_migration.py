"""Ports de la migration blue/green d'un index (S9 ; knowledge-base.md §5.4,
§13 ; ADR 0011 §4, §5).

``IndexableVersionReader`` est lu dans la transaction qui tient la ligne du
corpus ``FOR UPDATE`` (``StartIndexMigration``, ``ActivateSearchIndex``,
``AbandonIndexMigration``) :
aucune extraction ni publication, qui lisent le corpus ``FOR SHARE``, ne
s'intercale entre cette lecture et l'écriture qui en dépend.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_document import VersionStatus
from knowledge_base.domain.providers.indexing_transaction import IndexingUnitOfWork


@dataclass(frozen=True)
class IndexableVersion:
    """Version ``PUBLISHED`` ou ``EXTRACTED`` d'un document ``ACTIVE``, avec
    l'identité (``ChunkingPolicy.identity``) de chacun de ses lots de chunks."""

    document_id: UUID
    version_id: UUID
    version_status: VersionStatus
    policy_versions: frozenset[str]


class IndexableVersionReader(Protocol):
    async def list_indexable_versions(self) -> Sequence[IndexableVersion]:
        """Les versions qu'un nouvel index doit couvrir (backfill) ou qu'un
        index candidat doit couvrir avant son activation (I11)."""
        ...

    async def documents_awaiting_visibility(self) -> frozenset[UUID]:
        """Documents qui ont une synchronisation de visibilité
        (``sync_visibility``) ``PENDING`` ou ``RUNNING`` : leurs points
        peuvent encore être invisibles dans un index qui les a reçus."""
        ...

    async def documents_with_failed_visibility(self) -> frozenset[UUID]:
        """Documents dont la DERNIÈRE synchronisation de visibilité (la plus
        récemment créée, toutes révisions et générations confondues) est
        ``FAILED`` : aucune autre n'a convergé leurs points depuis, qui
        peuvent être restés invisibles dans chaque index vivant."""
        ...

    async def indexation_statuses(
        self, index_id: UUID
    ) -> Mapping[UUID, IndexationStatus]:
        """Statut de chaque indexation de l'index, par version, en une
        lecture (preuve de couverture d'I11, abandon d'une migration)."""
        ...


class MigrationUnitOfWork(IndexingUnitOfWork, Protocol):
    """La transaction de l'indexation, plus la lecture des versions
    indexables dans cette même transaction."""

    @property
    def versions(self) -> IndexableVersionReader: ...
