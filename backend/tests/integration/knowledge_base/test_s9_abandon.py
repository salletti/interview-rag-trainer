"""Abandon d'une migration blue/green (revue S9, I1 ; knowledge-base.md §5.4
``BUILDING --> RETIRED``, §13) sur PostgreSQL, Qdrant et work queue réels.

Une migration bloquée (couverture qui ne se complète pas, synchronisation en
échec, mauvaise cible) n'avait aucune issue : ni activation possible, ni
nouvelle migration (un seul index ``BUILDING``). L'abandon retire l'index,
annule son travail d'indexation (fencing compris), planifie la suppression de
sa collection et rend la main à une nouvelle migration ; l'index actif reste
servi sans interruption.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from knowledge_base.adapters.gateway.persistence.sqlalchemy_work_queue import (
    SqlAlchemyWorkLifecycle,
)
from knowledge_base.domain.exceptions.corpus_errors import (
    InvalidIndexTransitionError,
)
from runtime.database import SessionFactory
from runtime.settings import Settings
from tests.integration.knowledge_base.s9_support import (
    POLICY_SAME,
    POLICY_SHORT,
    S9Rig,
    documents_of,
    long_sheet,
    open_s9_rig,
)
from tests.unit.knowledge_base.use_cases.s9_support import PROFILE_B, PROFILE_C

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


async def test_an_abandoned_migration_retires_the_index_and_cancels_its_work(
    rig: S9Rig,
) -> None:
    first = await rig.publish(long_sheet("a"), title="Fiche a")
    second = await rig.publish(long_sheet("b"), title="Fiche b")
    new = await rig.start(PROFILE_B, POLICY_SAME)
    assert await rig.run_index(first.version_id, new) == "succeeded"  # COMPLETED

    await rig.abandon(new)

    assert await rig.statuses() == {rig.index_id: "ACTIVE", new: "RETIRED"}
    indexations = await rig.indexations(new)
    assert indexations[first.version_id]["status"] == "COMPLETED"
    assert indexations[second.version_id]["status"] == "CANCELLED"
    towards_new = {
        k: i["status"]
        for k, i in (await rig.items("index_version")).items()
        if k.endswith(str(new))
    }
    assert towards_new == {
        f"index_version:{first.version_id}:{new}": "SUCCEEDED",
        f"index_version:{second.version_id}:{new}": "CANCELLED",
    }
    # l'index actif sert toujours les deux documents
    result = await rig.search()
    assert result.index_id == rig.index_id
    assert documents_of(result) == {first.document_id, second.document_id}


async def test_the_rechunk_items_of_an_abandoned_index_are_cancelled(
    rig: S9Rig,
) -> None:
    await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SHORT)

    await rig.abandon(new)

    rechunks = await rig.items("rechunk_version")
    assert rechunks
    assert {i["status"] for i in rechunks.values()} == {"CANCELLED"}
    assert await rig.drain(index_id=new) == []  # plus rien à exécuter


async def test_an_attempt_in_flight_cannot_finalize_after_the_abandon(
    rig: S9Rig,
) -> None:
    published = await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    item_id = await rig.item_id(f"index_version:{published.version_id}:{new}")
    lifecycle = SqlAlchemyWorkLifecycle(rig.session_factory)
    claimed = await lifecycle.claim(item_id, "zombie", timedelta(minutes=5))
    assert claimed is not None

    await rig.abandon(new)

    async with rig.session_factory() as session, session.begin():
        finalized = await lifecycle.complete(session, claimed.id, claimed.fencing_token)
    assert finalized is False
    (row,) = await rig.rows(
        "SELECT status, fencing_token FROM knowledge_base.work_items WHERE id = :i",
        i=item_id,
    )
    assert row["status"] == "CANCELLED"
    assert row["fencing_token"] > claimed.fencing_token


async def test_the_collection_of_the_abandoned_index_is_dropped_by_its_item(
    rig: S9Rig,
) -> None:
    await rig.publish(long_sheet("a"))
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.drain(index_id=new, kinds={"index_version"})  # points écrits
    assert await rig.points(new)

    await rig.abandon(new)
    key = f"drop_index_collection:{new}"
    assert (await rig.items("drop_index_collection"))[key]["status"] == "PENDING"
    assert await rig.collection_exists(new)  # l'abandon n'appelle pas Qdrant

    assert await rig.run_key(key) == "succeeded"

    assert not await rig.collection_exists(new)
    assert await rig.collection_exists(rig.index_id)


async def test_the_drop_waits_for_the_configured_delay(rig: S9Rig) -> None:
    new = await rig.start(PROFILE_B, POLICY_SAME)

    await rig.abandon(new, drop_delay=timedelta(seconds=300))

    (row,) = await rig.rows(
        "SELECT available_at - created_at AS delay FROM knowledge_base.work_items "
        "WHERE dedup_key = :k",
        k=f"drop_index_collection:{new}",
    )
    assert row["delay"] >= timedelta(seconds=299)


async def test_a_new_migration_can_start_after_the_abandon(rig: S9Rig) -> None:
    published = await rig.publish(long_sheet("a"))
    blocked = await rig.start(PROFILE_B, POLICY_SAME)

    await rig.abandon(blocked)
    other = await rig.start(PROFILE_C, POLICY_SAME)

    assert other != blocked
    assert await rig.statuses() == {
        rig.index_id: "ACTIVE",
        blocked: "RETIRED",
        other: "BUILDING",
    }
    assert set(await rig.indexations(other)) == {published.version_id}


async def test_an_extraction_after_the_abandon_feeds_only_the_active_index(
    rig: S9Rig,
) -> None:
    new = await rig.start(PROFILE_B, POLICY_SAME)
    await rig.abandon(new)

    imported = await rig.import_extract(long_sheet("z"))

    assert imported.version_id not in await rig.indexations(new)
    assert imported.version_id in await rig.indexations(rig.index_id)


@pytest.mark.parametrize("which", ["active", "retired"])
async def test_an_index_that_is_not_building_cannot_be_abandoned(
    rig: S9Rig, which: str
) -> None:
    new = await rig.start(PROFILE_B, POLICY_SAME)
    if which == "retired":
        await rig.abandon(new)
    target = rig.index_id if which == "active" else new
    statuses = await rig.statuses()
    drops = await rig.items("drop_index_collection")

    with pytest.raises(InvalidIndexTransitionError):
        await rig.abandon(target)

    assert await rig.statuses() == statuses
    assert await rig.items("drop_index_collection") == drops
