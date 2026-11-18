"""Fencing et revérification métier de ``extract_version`` (I7, I14 ; S0 §5,
knowledge-base.md §7.3 point 5) : une tentative périmée, ou dont la version a
été supplantée, n'écrit aucun chunk (PostgreSQL réel).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    ExtractVersionWorkHandler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.providers.work_queue import FailureInfo
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    ExtractionRig,
    open_extraction_rig,
)
from tests.integration.knowledge_base.support import cancel_committed

pytestmark = pytest.mark.integration

COURSE = b"# Cours asyncio\n\nLes coroutines ne bloquent pas la boucle.\n"
V2 = b"# Cours asyncio\n\nLes coroutines et les taches.\n"


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ExtractionRig]:
    async with open_extraction_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


class BeforeFinalize:
    """Contexte d'exécution qui laisse faire un événement concurrent juste
    avant la finalisation, quand le travail hors transaction est terminé."""

    def __init__(
        self, inner: WorkContext, before: Callable[[], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._before = before

    @property
    def finalized(self) -> bool:
        return self._inner.finalized

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        await self._before()
        await self._inner.finalize(callback)


class InterferingHandler:
    """Enveloppe le vrai handler : mêmes ``kind``/file/tentatives, et l'événement
    concurrent a lieu avant ``ctx.finalize``."""

    def __init__(
        self,
        inner: ExtractVersionWorkHandler,
        before: Callable[[], Awaitable[None]],
    ) -> None:
        self._inner = inner
        self._before = before
        self.kind = inner.kind
        self.queue = inner.queue
        self.max_attempts = inner.max_attempts

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        proxy = cast(WorkContext, BeforeFinalize(ctx, self._before))
        await self._inner.execute(item, proxy)

    async def on_terminal_failure(
        self, session: AsyncSession, item: ClaimedWorkItem, error: FailureInfo
    ) -> None:
        await self._inner.on_terminal_failure(session, item, error)

    def as_work_handler(self) -> WorkHandler:
        return cast(WorkHandler, self)


async def set_sql(rig: ExtractionRig, sql: str, **params: object) -> None:
    async with rig.session_factory() as session, session.begin():
        await session.execute(text(sql), params)


async def test_a_stale_attempt_writes_no_unit_and_no_chunk(rig: ExtractionRig) -> None:
    imported = await rig.import_document(COURSE)

    async def cancel_meanwhile() -> None:
        await cancel_committed(
            rig.session_factory, f"extract_version:{imported.version_id}"
        )

    handler = InterferingHandler(rig.handler, cancel_meanwhile)

    outcome = await rig.run(imported.item_id, handler.as_work_handler())

    assert outcome == "stale"
    assert await rig.counts() == (0, 0)
    assert (await rig.version(imported.version_id))["status"] == "RECEIVED"
    assert (await rig.item(imported.item_id))["status"] == "CANCELLED"


async def test_a_version_superseded_before_the_run_leaves_no_chunk(
    rig: ExtractionRig,
) -> None:
    first = await rig.import_document(COURSE)
    response = await rig.imports.post_version(first.document_id, V2)
    assert response.status_code == 202

    outcome = await rig.run(first.item_id)

    assert outcome == "skipped"  # I6 : l'item de la version supplantée est annulé
    assert await rig.counts() == (0, 0)
    assert (await rig.version(first.version_id))["status"] == "DISCARDED"


async def test_a_version_discarded_without_cancelling_its_item_is_ignored(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(COURSE)
    await set_sql(
        rig,
        "UPDATE knowledge_base.document_versions SET status = 'DISCARDED' "
        "WHERE id = :id",
        id=imported.version_id,
    )

    outcome = await rig.run(imported.item_id)

    assert outcome == "succeeded"  # ignoré, pas une erreur : l'item se termine
    assert await rig.counts() == (0, 0)
    assert (await rig.version(imported.version_id))["status"] == "DISCARDED"


async def test_a_document_withdrawn_before_the_run_gets_no_chunk(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(COURSE)
    await set_sql(
        rig,
        "UPDATE knowledge_base.documents SET status = 'WITHDRAWN' WHERE id = :id",
        id=imported.document_id,
    )

    outcome = await rig.run(imported.item_id)

    assert outcome == "succeeded"
    assert await rig.counts() == (0, 0)
    assert (await rig.version(imported.version_id))["status"] == "RECEIVED"


async def test_the_version_discarded_during_the_run_is_not_extracted(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(COURSE)

    async def supersede() -> None:
        await set_sql(
            rig,
            "UPDATE knowledge_base.document_versions SET status = 'DISCARDED' "
            "WHERE id = :id",
            id=imported.version_id,
        )

    outcome = await rig.run(
        imported.item_id,
        InterferingHandler(rig.handler, supersede).as_work_handler(),
    )

    assert outcome == "succeeded"
    assert await rig.counts() == (0, 0)
    assert (await rig.version(imported.version_id))["status"] == "DISCARDED"
