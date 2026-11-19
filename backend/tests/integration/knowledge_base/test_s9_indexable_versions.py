"""Lectures SQL de la migration (revue S9) contre PostgreSQL réel :

- M1 : un seul prédicat « version indexable » (version ``PUBLISHED`` ou
  ``EXTRACTED`` d'un document ``ACTIVE``), partagé par la lecture de la
  migration (backfill, preuve de couverture) et par la réconciliation
  (rattrapage d'un index ``BUILDING``) : les deux rendent le même ensemble ;
  un document retiré n'y figure jamais ;
- I4 : ``documents_with_failed_visibility`` ne retient que les documents dont
  la DERNIÈRE synchronisation est ``FAILED`` (une génération suivante
  réussie, ou en attente, efface l'échec) ;
- M10 : ``indexation_statuses`` rend en une lecture le statut de chaque
  indexation d'un index.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from knowledge_base.adapters.gateway.persistence.sqlalchemy_reconciliation_store import (  # noqa: E501
    SqlAlchemyReconciliationStore,
)
from knowledge_base.adapters.gateway.persistence.sqlalchemy_unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from knowledge_base.domain.entities.indexation import IndexationStatus
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.s9_support import (
    POLICY_SAME,
    S9Rig,
    long_sheet,
    open_s9_rig,
)
from tests.unit.knowledge_base.use_cases.s9_support import PROFILE_B

pytestmark = pytest.mark.integration


@pytest.fixture
async def rig(
    integration_settings: Settings,
    session_factory: SessionFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[S9Rig]:
    async with open_s9_rig(
        integration_settings, session_factory, tmp_path, monkeypatch
    ) as opened:
        yield opened


async def failed_visibility(rig: S9Rig) -> frozenset[UUID]:
    async with SqlAlchemyUnitOfWork(rig.session_factory) as uow:
        return await uow.versions.documents_with_failed_visibility()


async def set_status(rig: S9Rig, dedup_key: str, status: str) -> None:
    async with rig.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE knowledge_base.work_items SET status = :s, "
                "completed_at = now() WHERE dedup_key = :k"
            ),
            {"s": status, "k": dedup_key},
        )


async def sync_key(rig: S9Rig, document_id: UUID) -> str:
    (key,) = [
        k
        for k, item in (await rig.items("sync_visibility")).items()
        if item["payload"]["document_id"] == str(document_id)
    ]
    return key


async def test_the_migration_and_the_reconciliation_read_the_same_versions(
    rig: S9Rig,
) -> None:
    published = await rig.publish(long_sheet("a"), title="Fiche a")
    extracted = await rig.import_extract(long_sheet("b"), title="Fiche b")
    withdrawn = await rig.publish(long_sheet("c"), title="Fiche c")
    await rig.withdraw(withdrawn.document_id)
    await rig.import_doc(long_sheet("d"), title="Fiche d")  # RECEIVED

    async with SqlAlchemyUnitOfWork(rig.session_factory) as uow:
        migration = {v.version_id for v in await uow.versions.list_indexable_versions()}
    store = SqlAlchemyReconciliationStore(rig.session_factory)
    reconciliation = {v.version_id for v in await store.indexable_versions()}

    assert migration == {published.version_id, extracted.version_id}
    assert reconciliation == migration


async def test_only_a_document_whose_last_sync_failed_is_reported(
    rig: S9Rig,
) -> None:
    failed = await rig.publish(long_sheet("a"), title="Fiche a")
    recovered = await rig.publish(long_sheet("b"), title="Fiche b")
    failed_key = await sync_key(rig, failed.document_id)
    recovered_key = await sync_key(rig, recovered.document_id)
    await set_status(rig, failed_key, "FAILED")
    await set_status(rig, recovered_key, "FAILED")
    async with rig.session_factory() as session, session.begin():
        # génération suivante, créée après l'échec puis réussie
        await session.execute(
            text(
                "INSERT INTO knowledge_base.work_items (id, kind, dedup_key, "
                "payload, status, completed_at, created_at) VALUES "
                "(gen_random_uuid(), 'sync_visibility', :k, "
                "jsonb_build_object('document_id', CAST(:d AS text), "
                "'visibility_revision', '1'), 'SUCCEEDED', now(), "
                "now() + interval '1 second')"
            ),
            {"k": f"{recovered_key}:2", "d": str(recovered.document_id)},
        )

    assert await failed_visibility(rig) == {failed.document_id}


async def test_the_indexation_statuses_of_an_index_are_read_at_once(
    rig: S9Rig,
) -> None:
    first = await rig.publish(long_sheet("a"), title="Fiche a")
    second = await rig.publish(long_sheet("b"), title="Fiche b")
    new = await rig.start(PROFILE_B, POLICY_SAME)
    assert await rig.run_index(first.version_id, new) == "succeeded"

    async with SqlAlchemyUnitOfWork(rig.session_factory) as uow:
        statuses = await uow.versions.indexation_statuses(new)

    assert statuses == {
        first.version_id: IndexationStatus.COMPLETED,
        second.version_id: IndexationStatus.PENDING,
    }
