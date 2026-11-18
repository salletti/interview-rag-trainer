"""Prolongation du lease pendant un travail long hors transaction (S0 §7) :
partagée par les handlers ``extract_version`` (parsing, chunking) et
``index_version`` (embeddings, upserts Qdrant)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext


async def keeping_lease[T](
    ctx: WorkContext, work: Coroutine[Any, Any, T], interval: timedelta | None
) -> T:
    """Exécute ``work`` en envoyant un heartbeat toutes les ``interval``
    (``None`` : aucun, pour les doubles de ``WorkContext`` qui n'en ont pas).

    Un heartbeat en échec (``StaleAttempt`` si la tentative est périmée)
    annule aussitôt ``work`` puis est relevé : une tentative zombie n'émet plus
    d'effet externe après son prochain heartbeat (knowledge-base.md §7.3.6).
    Le calcul déjà confié à un thread (``asyncio.to_thread``) va à son terme,
    mais son résultat est abandonné. Aucune des deux tâches ne survit à
    l'appel, y compris quand l'appelant est lui-même annulé."""
    if interval is None:
        return await work
    seconds = interval.total_seconds()

    async def beat() -> None:
        while True:
            await asyncio.sleep(seconds)
            await ctx.heartbeat()

    working = asyncio.create_task(work)
    beating = asyncio.create_task(beat())
    try:
        await asyncio.wait({working, beating}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        working.cancel()
        beating.cancel()
        await asyncio.wait({working, beating})
    # ``beat`` ne se termine que sur une exception. Elle prime sur le résultat
    # d'un travail fini au même tour de boucle : une tentative périmée ne doit
    # pas finaliser.
    if not beating.cancelled() and (refusal := beating.exception()) is not None:
        raise refusal
    return working.result()
