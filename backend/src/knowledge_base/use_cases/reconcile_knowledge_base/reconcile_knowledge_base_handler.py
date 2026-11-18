"""``ReconcileKnowledgeBase`` : un cycle de réconciliation entre PostgreSQL,
Qdrant et le volume des blobs (S8 ; ADR 0004 point 9, ADR 0010 point 9,
ADR 0012 point 6 ; knowledge-base.md §7.3, §8, §9, §10, §11, O14, O15).

Le cycle corrige les écarts que le cycle normal des work items ne corrige
pas ; il ne reprend PAS les items ``RUNNING`` dont le lease a expiré (tâche
``recover`` de S0). Ordre :

1. points de chaque index vivant (Qdrant lu AVANT PostgreSQL) ;
2. publications perdues, versions ``EXTRACTED`` sans indexation, manifests
   échantillonnés ;
3. items ``FAILED`` sans état métier (O14) et purges en échec ;
4. quarantaine, blobs orphelins, temporaires, clés d'idempotence (O15).

Chaque correction a sa propre transaction ; le travail long qu'elle demande
(réindexation, synchronisation, purge) est planifié en work item, pas exécuté
ici. Toute panne d'un service (Qdrant, PostgreSQL, volume) remonte : un cycle
raté n'est jamais « réussi », son work item est rejoué.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from knowledge_base.domain.entities.knowledge_corpus import SearchIndex
from knowledge_base.domain.providers.reconciliation_store import (
    ReconciliationUnitOfWork,
)
from knowledge_base.domain.services.reconciliation import DiscrepancyKind
from knowledge_base.use_cases.reconcile_knowledge_base.failed_work_reconciliation import (  # noqa: E501
    FailedWorkReconciliation,
)
from knowledge_base.use_cases.reconcile_knowledge_base.indexation_reconciliation import (  # noqa: E501
    IndexationReconciliation,
)
from knowledge_base.use_cases.reconcile_knowledge_base.point_reconciliation import (
    PointReconciliation,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    ReconcileKnowledgeBase,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconciliation_report import (
    ReconciliationReport,
)
from knowledge_base.use_cases.reconcile_knowledge_base.storage_housekeeping import (
    StorageHousekeeping,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationPolicy:
    """Réglages d'un cycle (``KnowledgeBaseSettings`` en production)."""

    manifest_sample_size: int
    blob_grace: timedelta
    idempotency_key_ttl: timedelta


class ReconcileKnowledgeBaseHandler:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], ReconciliationUnitOfWork],
        points: PointReconciliation,
        indexations: IndexationReconciliation,
        failed_work: FailedWorkReconciliation,
        storage: StorageHousekeeping,
        policy: ReconciliationPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._unit_of_work = unit_of_work
        self._points = points
        self._indexations = indexations
        self._failed_work = failed_work
        self._storage = storage
        self._policy = policy
        self._clock = clock

    async def handle(self, command: ReconcileKnowledgeBase) -> ReconciliationReport:
        started = self._clock()
        indexes = await self._live_indexes()
        counts: Counter[DiscrepancyKind] = Counter()
        counts += await self._points.run(indexes)
        counts += await self._indexations.publish_lost_publications()
        counts += await self._indexations.index_unindexed_versions(indexes)
        counts += await self._indexations.verify_manifests(
            self._policy.manifest_sample_size
        )
        counts += await self._failed_work.run()
        counts += await self._storage.run(
            grace=self._policy.blob_grace,
            idempotency_key_ttl=self._policy.idempotency_key_ttl,
        )
        report = ReconciliationReport(counts=dict(counts))
        elapsed = (self._clock() - started).total_seconds()
        if report.clean:
            logger.info(
                "réconciliation : aucun écart (%d index, %.2f s)", len(indexes), elapsed
            )
        else:
            logger.warning(
                "réconciliation : %s (%d index, %.2f s)", report, len(indexes), elapsed
            )
        return report

    async def _live_indexes(self) -> tuple[SearchIndex, ...]:
        """Index ``ACTIVE`` et ``BUILDING`` ; ceux d'un index ``RETIRED``
        partent avec sa collection (S9)."""
        async with self._unit_of_work() as uow:
            corpus = await uow.corpus.get_shared()
        return () if corpus is None else corpus.live_indexes()
