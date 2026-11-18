"""Prolongation du lease pendant un travail long hors transaction (S0 §7) :
partagée par les handlers ``extract_version`` (parsing, chunking) et
``index_version`` (embeddings)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowledge_base.adapters.controller.tasks.execution import WorkContext


async def keeping_lease[T](
    ctx: WorkContext, work: Awaitable[T], interval: timedelta | None
) -> T:
    """Attend ``work`` en envoyant un heartbeat toutes les ``interval``
    (``None`` : aucun, pour les doubles de ``WorkContext`` qui n'en ont pas).
    Un heartbeat refusé (``StaleAttempt``) est relevé dès la fin du travail,
    qui tourne dans un thread et ne peut être interrompu : la tentative
    s'arrête alors sans finaliser."""
    if interval is None:
        return await work
    seconds = interval.total_seconds()

    async def beat() -> None:
        while True:
            await asyncio.sleep(seconds)
            await ctx.heartbeat()

    beating = asyncio.create_task(beat())
    try:
        result = await work
    finally:
        beating.cancel()
        # Relève le StaleAttempt d'un heartbeat refusé pendant le travail ;
        # l'annulation ci-dessus, elle, est attendue.
        with contextlib.suppress(asyncio.CancelledError):
            await beating
    return result
