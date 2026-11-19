"""Ré-extraction après panne : mêmes identifiants (knowledge-base.md §3.4, §7.3).

Une tentative qui meurt après le travail hors transaction mais avant la
finalisation n'écrit rien ; la suivante produit les mêmes ``unit_id``,
``chunk_id`` et ``chunk_hash`` que n'importe quelle exécution du pipeline sur le
même contenu et la même politique (PostgreSQL réel).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.extract_version_handler import (
    ExtractVersionWorkHandler,
)
from knowledge_base.adapters.controller.tasks.registry import WorkHandler
from knowledge_base.adapters.controller.tasks.retry_policy import TransientWorkError
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from knowledge_base.domain.services.chunk_planner import ChunkPlanner
from knowledge_base.domain.services.knowledge_unit_segmenter import (
    KnowledgeUnitSegmenter,
)
from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.domain.value_objects.section_path import SectionPath
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.extraction_support import (
    POLICY,
    ExtractionRig,
    open_extraction_rig,
)
from tests.unit.knowledge_base.domain.services.support import COUNTER
from tests.unit.knowledge_base.markdown_lite import MarkdownLiteParser

pytestmark = pytest.mark.integration

QA_SHEET = (
    b"# Fiche FastAPI\n\n## Question : Qu'est-ce que Depends ?\n\nInjection.\n\n"
    b"## Question : Qu'est-ce que lifespan ?\n\nCycle de vie.\n"
)


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


class CrashBeforeFinalize:
    """Simule la mort du worker après le calcul, avant la finalisation."""

    def __init__(self, inner: ExtractVersionWorkHandler) -> None:
        self._inner = inner
        self.kind = inner.kind
        self.queue = inner.queue
        self.max_attempts = inner.max_attempts

    async def execute(self, item: ClaimedWorkItem, ctx: WorkContext) -> None:
        class Dying:
            finalized = False

            async def finalize(
                self, callback: Callable[[AsyncSession], Awaitable[None]]
            ) -> None:
                raise TransientWorkError("worker interrompu avant la finalisation")

        await self._inner.execute(item, cast(WorkContext, Dying()))


def expected(
    version_id: UUID, document_id: UUID
) -> tuple[list[UUID], list[tuple[UUID, str]]]:
    """Unités et chunks calculés hors base par le pipeline de domaine seul."""
    document = MarkdownLiteParser().parse(QA_SHEET, "text/markdown", "cours.md")
    units = KnowledgeUnitSegmenter(COUNTER, POLICY).segment(
        document, kind=DocumentKind.QA_SHEET, version_id=version_id
    )
    base = Citation(
        document_id=document_id,
        document_title="Python avancé",
        version_id=version_id,
        version_number=1,
        section_path=SectionPath(()),
        page_start=None,
        page_end=None,
        source_filename="cours.md",
    )
    planner = ChunkPlanner(COUNTER, POLICY)
    chunks = [c for u in units for c in planner.plan(u, base)]
    return [u.unit_id for u in units], [(c.chunk_id, c.chunk_hash) for c in chunks]


async def test_a_re_extraction_after_a_crash_yields_the_same_identifiers(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(QA_SHEET, kind="QA_SHEET")
    crashed = cast(WorkHandler, CrashBeforeFinalize(rig.handler))

    first = await rig.run(imported.item_id, crashed)

    assert first == "retry_scheduled"
    assert await rig.counts() == (0, 0)

    await asyncio.sleep(0.05)  # backoff de 10 ms (réglage de test)
    second = await rig.run(imported.item_id)

    assert second == "succeeded"
    unit_ids = [u["id"] for u in await rig.units(imported.version_id)]
    chunk_rows = await rig.chunks(imported.version_id)
    chunks = [(c["id"], c["chunk_hash"]) for c in chunk_rows]
    assert (unit_ids, chunks) == expected(imported.version_id, imported.document_id)
    assert len(unit_ids) == 2 and len(set(unit_ids)) == 2


async def test_executing_a_finished_item_again_duplicates_nothing(
    rig: ExtractionRig,
) -> None:
    imported = await rig.import_document(QA_SHEET, kind="QA_SHEET")
    await rig.run(imported.item_id)
    before = await rig.counts()

    assert await rig.run(imported.item_id) == "skipped"

    assert await rig.counts() == before == (2, 2)
