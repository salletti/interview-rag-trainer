"""Indexations confrontées à leurs versions et à l'index (S8 ; knowledge-base.md
§5.3, §8, §9, §13 ; I5, I8, I9).

- publication perdue : indexation ``COMPLETED`` dans l'index actif d'une
  version ``EXTRACTED`` publiable -> ``PublishDocumentVersion`` (mêmes gardes
  qu'à la finalisation). ``COMPLETED_NOT_PUBLISHED`` ne compte que les
  publications rattrapées (commit abouti) : un refus (constat périmé, ou
  refus persistant que ``is_publishable_completed`` ne prévoit pas) est
  journalisé sans compter, sinon un refus attendu rendrait chaque rapport
  « non propre » sans qu'aucun cycle ne puisse le corriger ;
- version ``EXTRACTED`` sans indexation dans un index vivant de la politique
  de son lot (code de journal ``EXTRACTED_WITHOUT_INDEXATION``) -> indexation
  ``PENDING`` et item ``index_version``, sous le verrou partagé du corpus
  (barrière blue/green, comme à l'extraction) ;
- manifest : au plus ``sample_size`` indexations ``COMPLETED`` revérifiées par
  cycle (chunks de PostgreSQL contre points de la version dans l'index) ; un
  écart rouvre l'indexation (``COMPLETED`` -> ``PENDING``) et replanifie
  ``index_version``, les points hors manifest sont supprimés.

Chaque correction relit l'état dans sa propre transaction avant d'écrire : un
constat périmé (version supplantée entre-temps) n'écrit rien.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from uuid import UUID

from knowledge_base.domain.entities.indexation import Indexation, IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import (
    IndexStatus,
    KnowledgeCorpus,
    SearchIndex,
)
from knowledge_base.domain.entities.knowledge_document import (
    DocumentStatus,
    KnowledgeDocument,
    VersionStatus,
)
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from knowledge_base.domain.exceptions.document_errors import (
    KnowledgeDocumentError,
    VersionNotFoundError,
)
from knowledge_base.domain.providers.knowledge_document_repository import (
    ConcurrentModificationError,
)
from knowledge_base.domain.providers.point_inventory import ReconcilableIndex
from knowledge_base.domain.providers.reconciliation_store import (
    CompletedIndexation,
    ReconciliationStore,
    ReconciliationUnitOfWork,
)
from knowledge_base.domain.services.reconciliation import (
    DiscrepancyKind,
    compare_manifest,
    indexes_missing_indexation,
    is_publishable_completed,
    points_beyond_manifest,
)
from knowledge_base.domain.value_objects.index_manifest import IndexManifest
from knowledge_base.domain.value_objects.indexed_point import PointSelector
from knowledge_base.use_cases.errors import DocumentNotFoundError
from knowledge_base.use_cases.indexing_steps import reschedule_indexation
from knowledge_base.use_cases.publish_document_version.errors import (
    PublicationRefusedError,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_command import (  # noqa: E501
    PublishDocumentVersion,
)
from knowledge_base.use_cases.publish_document_version.publish_document_version_handler import (  # noqa: E501
    PublishDocumentVersionHandler,
)

logger = logging.getLogger(__name__)

Sampler = Callable[[Sequence[CompletedIndexation], int], Sequence[CompletedIndexation]]

_INDEXABLE_VERSIONS = frozenset({VersionStatus.EXTRACTED, VersionStatus.PUBLISHED})


class IndexationReconciliation:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ReconciliationUnitOfWork],
        store: ReconciliationStore,
        vector_index: ReconcilableIndex,
        publisher: PublishDocumentVersionHandler,
        sampler: Sampler,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._store = store
        self._index = vector_index
        self._publisher = publisher
        self._sampler = sampler

    # --- publications perdues ---------------------------------------------------------

    async def publish_lost_publications(self) -> Counter[DiscrepancyKind]:
        counts: Counter[DiscrepancyKind] = Counter()
        for candidate in await self._store.publication_candidates():
            if not is_publishable_completed(
                document_status=candidate.document_status,
                version_status=candidate.version_status,
                version_number=candidate.version_number,
                published_number=candidate.published_number,
                indexation_status=candidate.indexation_status,
                index_status=candidate.index_status,
            ):
                continue
            if await self._publish(candidate.document_id, candidate.version_id):
                counts[DiscrepancyKind.COMPLETED_NOT_PUBLISHED] += 1
        return counts

    async def _publish(self, document_id: UUID, version_id: UUID) -> bool:
        """Vrai si la publication perdue est rattrapée (commit abouti)."""
        try:
            async with self._unit_of_work() as uow:
                await self._publisher.apply(
                    uow, PublishDocumentVersion(document_id, version_id)
                )
                await uow.commit()
        except (
            PublicationRefusedError,
            KnowledgeDocumentError,
            DocumentNotFoundError,
            ConcurrentModificationError,
        ) as refusal:
            # Constat périmé (le cycle suivant ne le reverra pas) ou refus
            # persistant : rien que ce cycle puisse corriger, donc pas un
            # écart compté ; le journal garde la trace.
            logger.warning(
                "indexation COMPLETED de la version %s non publiée : %s",
                version_id,
                refusal,
            )
            return False
        logger.warning(
            "publication perdue de la version %s rattrapée par la réconciliation",
            version_id,
        )
        return True

    # --- versions EXTRACTED sans indexation -------------------------------------------

    async def index_unindexed_versions(
        self, indexes: Collection[SearchIndex]
    ) -> Counter[DiscrepancyKind]:
        counts: Counter[DiscrepancyKind] = Counter()
        for version in await self._store.extracted_versions():
            eligible = {
                index.index_id
                for index in indexes
                if index.chunking_policy.identity in version.policy_versions
            }
            missing = indexes_missing_indexation(
                document_status=version.document_status,
                version_status=version.version_status,
                live_index_ids=eligible,
                indexed_index_ids=version.indexed_index_ids,
            )
            if not missing:
                continue
            counts[DiscrepancyKind.EXTRACTED_WITHOUT_INDEXATION] += 1
            await self._schedule_indexations(
                version.document_id, version.version_id, missing
            )
        return counts

    async def _schedule_indexations(
        self, document_id: UUID, version_id: UUID, index_ids: Collection[UUID]
    ) -> None:
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_shared()
            document = await uow.documents.get(document_id)
            if not _is_extracted(document, version_id):
                return
            for index_id in sorted(index_ids):
                index = _live_index(corpus, index_id)
                if index is None or await uow.indexations.get(version_id, index_id):
                    continue
                await uow.indexations.add(
                    Indexation.pending(
                        version_id=version_id,
                        index_id=index_id,
                        chunking_policy_version=index.chunking_policy.identity,
                    )
                )
                await reschedule_indexation(
                    uow.work_queue, document_id, version_id, index_id
                )
                logger.warning(
                    "version %s EXTRACTED sans indexation dans l'index %s : "
                    "indexation planifiée",
                    version_id,
                    index_id,
                )
            await uow.commit()

    # --- manifests --------------------------------------------------------------------

    async def verify_manifests(self, sample_size: int) -> Counter[DiscrepancyKind]:
        counts: Counter[DiscrepancyKind] = Counter()
        if sample_size <= 0:
            return counts
        candidates = await self._store.completed_indexations()
        for indexation in self._sampler(candidates, min(sample_size, len(candidates))):
            counts += await self._verify(indexation)
        return counts

    async def _verify(
        self, indexation: CompletedIndexation
    ) -> Counter[DiscrepancyKind]:
        counts: Counter[DiscrepancyKind] = Counter()
        async with self._unit_of_work() as uow:
            chunks = await uow.chunks.list_chunks(
                indexation.version_id, indexation.chunking_policy_version
            )
        expected = IndexManifest.for_chunks(indexation.index_id, chunks)
        found = await self._index.points_of(
            indexation.index_id,
            PointSelector(indexation.document_id, indexation.version_id),
        )
        extra = points_beyond_manifest(expected, found.keys())
        if extra:
            await self._index.delete_point_ids(indexation.index_id, extra)
            counts[DiscrepancyKind.ORPHAN_POINTS] += len(extra)
            logger.warning(
                "version %s, index %s : %d point(s) hors manifest supprimé(s)",
                indexation.version_id,
                indexation.index_id,
                len(extra),
            )
        drift = compare_manifest(expected, indexation.manifest_digest, found)
        if drift is not None:
            counts[DiscrepancyKind.MANIFEST_DRIFT] += 1
            logger.warning(
                "manifest de la version %s dans l'index %s non vérifié : %d "
                "point(s) manquant(s), %d empreinte(s) différente(s), digest "
                "enregistré %s ; indexation rouverte",
                indexation.version_id,
                indexation.index_id,
                len(drift.missing),
                len(drift.different),
                "périmé" if drift.digest_changed else "conforme",
            )
            await self._reopen(indexation)
        return counts

    async def _reopen(self, completed: CompletedIndexation) -> None:
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_shared()
            document = await uow.documents.get(completed.document_id)
            indexation = await uow.indexations.get(
                completed.version_id, completed.index_id
            )
            if (
                indexation is None
                or indexation.status is not IndexationStatus.COMPLETED
                or indexation.manifest_digest != completed.manifest_digest
                or not _is_indexable(document, completed.version_id)
                or _live_index(corpus, completed.index_id) is None
            ):
                return
            indexation.reopen()
            await uow.indexations.save(indexation)
            await reschedule_indexation(
                uow.work_queue,
                completed.document_id,
                completed.version_id,
                completed.index_id,
            )
            await uow.commit()


def _version_status(
    document: KnowledgeDocument | None, version_id: UUID
) -> VersionStatus | None:
    if document is None or document.status is not DocumentStatus.ACTIVE:
        return None
    try:
        return document.get_version(version_id).status
    except VersionNotFoundError:
        return None


def _is_extracted(document: KnowledgeDocument | None, version_id: UUID) -> bool:
    return _version_status(document, version_id) is VersionStatus.EXTRACTED


def _is_indexable(document: KnowledgeDocument | None, version_id: UUID) -> bool:
    return _version_status(document, version_id) in _INDEXABLE_VERSIONS


def _live_index(corpus: KnowledgeCorpus | None, index_id: UUID) -> SearchIndex | None:
    if corpus is None:
        return None
    try:
        index = corpus.get_index(index_id)
    except UnknownIndexError:
        return None
    return None if index.status is IndexStatus.RETIRED else index
