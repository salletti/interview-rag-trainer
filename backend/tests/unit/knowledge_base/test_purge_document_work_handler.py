"""Handler de work item ``purge_document`` : pont entre la work queue (S0 §5) et
``PurgeDocument`` (S7 ; knowledge-base.md §6, §7.2).

API figée (``knowledge_base.adapters.controller.tasks.purge_document_handler``),
même forme que ``index_version_handler`` et ``sync_visibility_handler`` :

- ``PurgeDocumentWorkHandler(use_case: PurgeDocumentHandler, transaction:
  Callable[[AsyncSession], <transaction de purge>], *,
  heartbeat_interval: timedelta | None = None)`` ; ``kind = "purge_document"``,
  ``queue = "knowledge_base.ingestion"`` (aucune file nouvelle), ``max_attempts``
  >= 1. ``execute`` : payload ``document_id`` ; ``use_case.prepare`` hors
  transaction (sous ``keeping_lease`` : un heartbeat refusé l'interrompt avant
  l'effet suivant) ; puis UN SEUL ``ctx.finalize`` dont le callback fait
  ``use_case.apply`` (rien à écrire si ``prepare`` rend ``None``, mais l'item
  doit se terminer). Une erreur de ``prepare`` (Qdrant, volume) remonte telle
  quelle : transitoire pour S0, jamais de finalisation, jamais ``SUCCEEDED``.
  Payload illisible : ``TerminalWorkError("INVALID_PAYLOAD")``.
- ``build_purge_document_handler(session_factory, settings, *, vector_index)``
  composition de production (volume ``settings.blob_root``) ; ne se connecte à
  rien. ``build_work_handlers`` (``dependencies``) l'ajoute à ses handlers.
  ``tests/unit/test_worker_knowledge_base.py`` affirme aujourd'hui l'ensemble
  exact ``{extract_version, index_version, sync_visibility}`` : il devra
  inclure ``purge_document`` (changement de ce test, pas de celui-ci).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID, uuid4

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_base import KnowledgeBaseSettings
from knowledge_base.adapters.controller.dependencies import build_work_handlers
from knowledge_base.adapters.controller.tasks.execution import WorkContext
from knowledge_base.adapters.controller.tasks.purge_document_handler import (
    PurgeDocumentWorkHandler,
)
from knowledge_base.adapters.controller.tasks.registry import HandlerRegistry
from knowledge_base.adapters.controller.tasks.retry_policy import TerminalWorkError
from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    ClaimedWorkItem,
)
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s7_support import (
    PURGED_AT,
    PurgeUnitOfWork,
    S7Rig,
    make_s7_rig,
)

SESSION = cast(AsyncSession, object())


class FakeContext:
    """Rejoue ``WorkContext.finalize`` : callback dans une transaction que l'on
    valide, une seule fois, sur succès du callback."""

    def __init__(self, rig: S7Rig) -> None:
        self._rig = rig
        self.current: PurgeUnitOfWork | None = None
        self.finalize_calls = 0

    async def finalize(
        self, callback: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        self.finalize_calls += 1
        async with self._rig.purge_unit_of_work() as uow:
            self.current = uow
            await callback(SESSION)
            await uow.commit()


def as_context(ctx: FakeContext) -> WorkContext:
    return cast(WorkContext, ctx)


def purge_handler(rig: S7Rig, ctx: FakeContext) -> PurgeDocumentWorkHandler:
    def transaction(session: AsyncSession) -> PurgeUnitOfWork:
        assert session is SESSION
        assert ctx.current is not None
        return ctx.current

    return PurgeDocumentWorkHandler(
        use_case=rig.purge_handler(), transaction=transaction
    )


def claimed(payload: dict[str, str]) -> ClaimedWorkItem:
    return ClaimedWorkItem(
        id=uuid4(),
        kind="purge_document",
        payload=payload,
        attempts=1,
        max_attempts=5,
        fencing_token=1,
        correlation_id=None,
    )


async def withdrawn_and_indexed(rig: S7Rig) -> tuple[ClaimedWorkItem, UUID, UUID]:
    """Item de purge, document retiré et publié, et l'index où il a des points."""
    index_id = await rig.base.provision()
    command = await rig.published(index_id, qa_sheet())
    rig.mark_withdrawn(command.document_id)
    assert rig.points_of_document(command.document_id, index_id)
    return (
        claimed({"document_id": str(command.document_id)}),
        command.document_id,
        index_id,
    )


def test_the_handler_serves_purge_document_on_the_ingestion_queue() -> None:
    rig = make_s7_rig()
    handler = purge_handler(rig, FakeContext(rig))

    registry = HandlerRegistry([handler])

    assert registry.kinds_by_queue() == {
        "knowledge_base.ingestion": frozenset({"purge_document"})
    }
    assert registry.max_attempts_by_kind()["purge_document"] >= 1


def test_production_composition_registers_purge_document() -> None:
    handlers = build_work_handlers(
        async_sessionmaker(),
        AsyncQdrantClient(url="http://localhost:1", check_compatibility=False),
        KnowledgeBaseSettings(_env_file=None),
    )

    registry = HandlerRegistry(handlers)

    assert registry.get("purge_document").queue == "knowledge_base.ingestion"
    assert "purge_document" in registry.kinds_by_queue()["knowledge_base.ingestion"]


async def test_execute_purges_then_finalizes_exactly_once() -> None:
    rig = make_s7_rig()
    ctx = FakeContext(rig)
    item, document_id, index_id = await withdrawn_and_indexed(rig)

    await purge_handler(rig, ctx).execute(item, as_context(ctx))

    assert ctx.finalize_calls == 1
    assert rig.points_of_document(document_id, index_id) == []
    assert rig.ledger.purged_at == {document_id: PURGED_AT}
    assert rig.versions_with_chunks() == set()


async def test_execute_finalizes_even_when_there_is_nothing_to_purge() -> None:
    rig = make_s7_rig()
    ctx = FakeContext(rig)

    await purge_handler(rig, ctx).execute(
        claimed({"document_id": str(uuid4())}), as_context(ctx)
    )

    assert ctx.finalize_calls == 1
    assert rig.ledger.purged_at == {}


async def test_execute_never_finalizes_when_qdrant_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_s7_rig()
    ctx = FakeContext(rig)
    item, _document_id, _index_id = await withdrawn_and_indexed(rig)

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Qdrant indisponible")

    monkeypatch.setattr(rig.base.vector, "delete_points", unavailable)

    with pytest.raises(RuntimeError, match="Qdrant indisponible"):
        await purge_handler(rig, ctx).execute(item, as_context(ctx))

    assert ctx.finalize_calls == 0
    assert rig.ledger.purged_at == {}


@pytest.mark.parametrize(
    "payload", [{}, {"document_id": "pas-un-uuid"}, {"doc": str(uuid4())}]
)
async def test_an_unreadable_payload_is_a_terminal_invalid_payload(
    payload: dict[str, str],
) -> None:
    rig = make_s7_rig()
    ctx = FakeContext(rig)

    with pytest.raises(TerminalWorkError) as raised:
        await purge_handler(rig, ctx).execute(claimed(payload), as_context(ctx))

    assert raised.value.code == "INVALID_PAYLOAD"
    assert ctx.finalize_calls == 0
