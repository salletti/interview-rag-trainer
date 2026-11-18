"""Points de l'index confrontés à l'état SQL de leur document (S8 ;
knowledge-base.md §7.3 condition 6, §9, §10, §11).

Ordre imposé : Qdrant d'abord (parcours complet de chaque index vivant), puis
PostgreSQL. Un point écrit après la lecture SQL serait sinon pris pour un
orphelin et supprimé ; lu avant, son document et sa version sont forcément
déjà commités (une version ne disparaît jamais de PostgreSQL), donc jugés.

Corrections : points sans propriétaire supprimés par sélection (version ou
document) ; synchronisation de visibilité ou purge replanifiée sous une
nouvelle génération de leur clé, sauf si un item du même travail est déjà en
attente pour ce document.

Une transaction par document replanifié, qui relit le document avant
d'écrire : un document retiré depuis le constat n'a plus de synchronisation
à refaire (sa purge s'en charge). Le retrait et la purge (``purged_at``,
jamais effacée) sont définitifs (O11) : un constat « retiré et purgé » ne se
périme pas. Un document en échec n'empêche pas les suivants ; les échecs
sont relevés ensemble à la fin, le cycle n'est pas « réussi ».
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping
from uuid import UUID

from knowledge_base.domain.entities.knowledge_corpus import SearchIndex
from knowledge_base.domain.entities.knowledge_document import DocumentStatus
from knowledge_base.domain.providers.point_inventory import ReconcilableIndex
from knowledge_base.domain.providers.reconciliation_store import (
    ReconciliationStore,
    ReconciliationUnitOfWork,
)
from knowledge_base.domain.services.reconciliation import (
    DiscrepancyKind,
    DocumentFacts,
    PointFacts,
    PointVerdict,
    judge_point,
    plan_point_corrections,
)
from knowledge_base.use_cases.indexing_steps import (
    PURGE_DOCUMENT,
    SYNC_VISIBILITY,
    reschedule_purge,
    reschedule_visibility_sync,
)

logger = logging.getLogger(__name__)


# Points d'un index regroupés par (document, version, visibilité) : la
# mémoire du cycle croît avec le nombre de versions, pas de points.
PointGroups = Counter[PointFacts]


class PointReconciliation:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ReconciliationUnitOfWork],
        store: ReconciliationStore,
        vector_index: ReconcilableIndex,
        scan_batch_size: int,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._store = store
        self._index = vector_index
        self._scan_batch_size = scan_batch_size

    async def run(self, indexes: Collection[SearchIndex]) -> Counter[DiscrepancyKind]:
        scanned = {index.index_id: await self._scan(index) for index in indexes}
        documents = await self._store.documents(
            {facts.document_id for groups in scanned.values() for facts in groups}
        )
        syncing = await self._store.documents_with_live_work(SYNC_VISIBILITY)
        purging = await self._store.documents_with_live_work(PURGE_DOCUMENT)

        counts: Counter[DiscrepancyKind] = Counter()
        resync: set[UUID] = set()
        repurge: set[UUID] = set()
        for index_id, groups in scanned.items():
            counts += _count(groups, documents, syncing=syncing, purging=purging)
            plan = plan_point_corrections(groups, documents)
            for selector in plan.delete:
                await self._index.delete_points(index_id, selector)
            resync |= plan.resync - syncing
            repurge |= plan.repurge - purging
        await self._reschedule(resync, repurge)
        return counts

    async def _scan(self, index: SearchIndex) -> PointGroups:
        groups: PointGroups = Counter()
        async for point in self._index.scan_points(
            index.index_id, batch_size=self._scan_batch_size
        ):
            groups[PointFacts(point.document_id, point.version_id, point.visible)] += 1
        return groups

    async def _reschedule(self, resync: set[UUID], repurge: set[UUID]) -> None:
        failures: list[Exception] = []
        for document_id in sorted(resync):
            await _isolated(document_id, self._resync(document_id), failures)
        for document_id in sorted(repurge):
            await _isolated(document_id, self._repurge(document_id), failures)
        if failures:
            raise ExceptionGroup(
                f"{len(failures)} document(s) non replanifié(s)", failures
            )

    async def _resync(self, document_id: UUID) -> None:
        async with self._unit_of_work() as uow:
            document = await uow.documents.get(document_id)
            if (
                document is None
                or document.status is not DocumentStatus.ACTIVE
                or document.published_version_id is None
            ):
                logger.info(
                    "document %s : plus de version publiée visible, "
                    "synchronisation non replanifiée",
                    document_id,
                )
                return
            await reschedule_visibility_sync(
                uow.work_queue, document_id, document.visibility_revision
            )
            await uow.commit()
        logger.warning(
            "document %s : points publiés invisibles, synchronisation "
            "replanifiée (révision %d)",
            document_id,
            document.visibility_revision,
        )

    async def _repurge(self, document_id: UUID) -> None:
        async with self._unit_of_work() as uow:
            document = await uow.documents.get(document_id)
            if document is None or document.status is not DocumentStatus.WITHDRAWN:
                logger.info(
                    "document %s : pas un document retiré, purge non replanifiée",
                    document_id,
                )
                return
            await reschedule_purge(uow.work_queue, document_id)
            await uow.commit()
        logger.warning(
            "document %s retiré et purgé : points réécrits après la purge, "
            "purge replanifiée",
            document_id,
        )


async def _isolated(
    document_id: UUID, correction: Awaitable[None], failures: list[Exception]
) -> None:
    """La correction d'un document ; son échec est relevé sans arrêter les
    corrections des autres documents (chacune a sa transaction)."""
    try:
        await correction
    except Exception as error:
        logger.warning(
            "document %s : replanification en échec (%s: %s)",
            document_id,
            type(error).__name__,
            error,
        )
        failures.append(error)


def _count(
    groups: PointGroups,
    documents: Mapping[UUID, DocumentFacts],
    *,
    syncing: frozenset[UUID],
    purging: frozenset[UUID],
) -> Counter[DiscrepancyKind]:
    """Points jugés en écart ; ceux qu'un travail en attente corrigera déjà
    (synchronisation ou purge planifiée) ne le sont pas."""
    counts: Counter[DiscrepancyKind] = Counter()
    for facts, points in groups.items():
        match judge_point(facts, documents.get(facts.document_id)):
            case PointVerdict.ORPHAN:
                counts[DiscrepancyKind.ORPHAN_POINTS] += points
            case PointVerdict.RESYNC if facts.document_id not in syncing:
                counts[DiscrepancyKind.HIDDEN_PUBLISHED_POINTS] += points
            case PointVerdict.REPURGE if facts.document_id not in purging:
                counts[DiscrepancyKind.WITHDRAWN_DOCUMENT_POINTS] += points
            case _:
                pass
    return counts
