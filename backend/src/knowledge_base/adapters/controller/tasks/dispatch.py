"""Tâches périodiques ``dispatch`` et ``recover`` (S0 §8.2, §8.3)."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from knowledge_base.adapters.controller.tasks.resources import (
    KnowledgeBaseWorkerResources,
)
from knowledge_base.adapters.controller.tasks.retry_policy import compute_backoff
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    QueueCapacity,
    SqlAlchemyWorkLifecycle,
)
from knowledge_base.frameworks.configuration.settings import KnowledgeBaseSettings

logger = logging.getLogger(__name__)

Publish = Callable[[UUID, str], None]


async def dispatch_pending(
    resources: KnowledgeBaseWorkerResources,
    settings: KnowledgeBaseSettings,
    *,
    publish: Publish,
) -> int:
    """Sélectionne et marque les items à réveiller (transaction courte,
    commitée), **puis** appelle ``publish(item_id, queue)`` pour chacun. Seuls
    les ``kind`` enregistrés sont dispatchés."""
    registry = resources.registry
    capacities = [
        QueueCapacity(queue, kinds, settings.work_max_in_flight)
        for queue, kinds in sorted(registry.kinds_by_queue().items())
    ]
    if not capacities:
        return 0
    lifecycle = SqlAlchemyWorkLifecycle(resources.session_factory)
    selected = await lifecycle.select_dispatchable(
        capacities, settings.work_dispatch_batch, settings.redispatch_after
    )
    for item_id, kind in selected:
        publish(item_id, registry.get(kind).queue)
    if selected:
        logger.info("dispatch : %d item(s) notifié(s)", len(selected))
    return len(selected)


async def recover_expired_leases(
    resources: KnowledgeBaseWorkerResources, settings: KnowledgeBaseSettings
) -> int:
    """Reprend un lot de leases expirés (crash, OOM, ``time_limit``)."""

    def backoff(attempts: int) -> timedelta:
        return compute_backoff(
            attempts,
            base=settings.backoff_base,
            cap=settings.backoff_cap,
            uniform=random.uniform,
        )

    lifecycle = SqlAlchemyWorkLifecycle(resources.session_factory)
    recovered = await lifecycle.recover_expired(
        settings.work_dispatch_batch, backoff=backoff
    )
    if recovered:
        logger.warning("recover : %d lease(s) expiré(s) repris", recovered)
    return recovered
