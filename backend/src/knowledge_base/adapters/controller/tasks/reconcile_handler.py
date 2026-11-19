"""Handler du ``kind`` ``reconcile_knowledge_base`` : un cycle de
``ReconcileKnowledgeBase`` exécuté comme un work item (S8 ; ADR 0004 points 2,
7, 9, 10 ; S0 §5, §8.5).

Planification : la tâche beat ``knowledge_base.work.reconcile`` (file par
défaut, jamais derrière une extraction) n'exécute pas le cycle, elle
l'ENREGISTRE (``enqueue_reconciliation``) : un item par créneau de
``reconcile_interval`` (clé ``reconcile_knowledge_base:<créneau>``, beat
redémarré : même item), et aucun tant qu'un cycle est encore ``PENDING`` ou
``RUNNING`` : une file d'ingestion occupée (longue extraction) ne cumule pas
un cycle par créneau manqué, le cycle en attente fera le travail de tous.
L'item suit le cycle de vie commun (lease, fencing, backoff) et s'exécute sur
la file d'ingestion : sérialisé avec les extractions, indexations et purges
(``--concurrency=1``), il ne court avec aucun traitement en vol. Un cycle
``FAILED`` reste en base, mais n'est ni listé ni rejouable (``ListFailures``,
``RetryIngestion``) : le créneau suivant en planifie un neuf.

``execute`` : le cycle hors transaction, sous ``keeping_lease`` (un cycle sur
un grand corpus peut dépasser le lease ; un heartbeat refusé l'interrompt),
puis UN ``ctx.finalize`` qui n'écrit rien (chaque correction a eu sa propre
transaction). Toute erreur du cycle remonte telle quelle : transitoire pour
S0, jamais de finalisation. Pas de hook d'échec : aucun agrégat à marquer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base.adapters.controller.composition import (
    build_reconcile_knowledge_base_handler,
)
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    INGESTION_QUEUE,
)
from knowledge_base.adapters.controller.tasks.lease import keeping_lease
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkQueue,
)
from knowledge_base.domain.providers.point_inventory import ReconcilableIndex
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_command import (  # noqa: E501
    RECONCILE_KNOWLEDGE_BASE,
    ReconcileKnowledgeBase,
)
from knowledge_base.use_cases.reconcile_knowledge_base.reconcile_knowledge_base_handler import (  # noqa: E501
    ReconcileKnowledgeBaseHandler,
)

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext
    from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
        ClaimedWorkItem,
    )

__all__ = [
    "RECONCILE_KNOWLEDGE_BASE",
    "ReconcileWorkHandler",
    "build_reconcile_work_handler",
    "enqueue_reconciliation",
    "reconciliation_key",
]


class ReconcileWorkHandler:
    kind: ClassVar[str] = RECONCILE_KNOWLEDGE_BASE
    queue: ClassVar[str] = INGESTION_QUEUE
    # Trois tentatives : au-delà, l'item est FAILED (en base, non rejoué) et
    # le créneau suivant planifie un nouveau cycle.
    max_attempts: ClassVar[int] = 3

    def __init__(
        self,
        use_case: ReconcileKnowledgeBaseHandler,
        *,
        heartbeat_interval: timedelta | None = None,
    ) -> None:
        """``heartbeat_interval`` : ``None`` désactive les heartbeats (doubles
        de ``WorkContext`` des tests unitaires qui n'en ont pas)."""
        self._use_case = use_case
        self._heartbeat_interval = heartbeat_interval

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        await keeping_lease(
            ctx,
            self._use_case.handle(ReconcileKnowledgeBase()),
            self._heartbeat_interval,
        )

        async def nothing_to_write(session: AsyncSession) -> None:
            return None

        await ctx.finalize(nothing_to_write)


def reconciliation_key(now: datetime, interval: timedelta) -> str:
    """Clé du créneau qui contient ``now`` : ``floor(now / intervalle)``."""
    slot = int(now.timestamp() // interval.total_seconds())
    return f"{RECONCILE_KNOWLEDGE_BASE}:{slot}"


async def enqueue_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    now: datetime | None = None,
) -> UUID:
    """Enregistre le cycle du créneau courant (ou celui de ``now``) ; rend
    l'item existant si ce créneau a déjà le sien, ou le cycle encore en
    attente ou en cours (d'un créneau antérieur) s'il y en a un.

    Deux beats simultanés (erreur de déploiement, ADR 0004) pourraient
    chacun ne voir aucun cycle vivant et en créer deux de créneaux voisins :
    un cycle en double est inoffensif (corrections relues et idempotentes)."""
    key = reconciliation_key(now or datetime.now(UTC), settings.reconcile_interval)
    async with session_factory() as session, session.begin():
        queue = SqlAlchemyWorkQueue(session)
        live = await queue.live_item(RECONCILE_KNOWLEDGE_BASE)
        if live is not None:
            return live
        return await queue.enqueue(RECONCILE_KNOWLEDGE_BASE, key, {})


def build_reconcile_work_handler(
    session_factory: async_sessionmaker[AsyncSession],
    settings: KnowledgeBaseSettings,
    *,
    vector_index: ReconcilableIndex,
) -> ReconcileWorkHandler:
    """Composition de production ; ne se connecte à rien."""
    return ReconcileWorkHandler(
        build_reconcile_knowledge_base_handler(session_factory, vector_index, settings),
        heartbeat_interval=settings.heartbeat_interval,
    )
