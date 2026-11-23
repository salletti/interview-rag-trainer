"""Abandon d'une migration blue/green : ``AbandonIndexMigration`` (revue S9 ;
knowledge-base.md §5.4 ``BUILDING --> RETIRED``, §13).

Une migration bloquée (couverture qui ne se complète pas, mauvaise cible) n'a
d'autre issue que l'abandon. Contrat de
``AbandonIndexMigrationHandler(*, unit_of_work, clock, drop_delay)
.handle(AbandonIndexMigration(index_id))`` :

1. UNE transaction, ouverte par la lecture du corpus ``FOR UPDATE`` (la
   barrière de §13) ; aucun appel Qdrant ;
2. l'index ``BUILDING`` passe ``RETIRED`` (``KnowledgeCorpus.retire_index``) ;
   un index ``ACTIVE`` ou ``RETIRED`` est refusé
   (``InvalidIndexTransitionError``), un index inconnu aussi
   (``UnknownIndexError``), sans aucune écriture ;
3. ses indexations ``PENDING`` et ``FAILED`` passent ``CANCELLED`` (une
   ``COMPLETED`` reste un fait) ; ses items ``index_version`` et
   ``rechunk_version``, générations comprises, sont annulés (fencing : une
   tentative en cours ne finalise plus). Les ``sync_visibility`` ne visent
   aucun index (ils convergent chaque index vivant) : ils restent, et
   ignorent désormais l'index retiré ;
4. ``drop_index_collection`` de l'index, disponible après ``drop_delay``.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.exceptions.corpus_errors import (
    InvalidIndexTransitionError,
    UnknownIndexError,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from tests.unit.knowledge_base.use_cases.fakes import FakeWorkItem
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s9_support import (
    LATER,
    POLICY_SAME,
    POLICY_SHORT,
    PROFILE_B,
    PROFILE_C,
    S9Rig,
    contract,
    make_s9_rig,
)

ABANDON_HANDLER = (
    "knowledge_base.use_cases.abandon_index_migration.abandon_index_migration_handler"
)


async def building_with_backfill(rig: S9Rig, policy: object = POLICY_SAME) -> UUID:
    """Index actif, deux documents publiés, migration démarrée (backfill
    planifié, rien d'exécuté)."""
    await rig.provision()
    for tag in "ab":
        await rig.publish_document(qa_sheet(2, tag=tag))
    return await rig.start(PROFILE_B, policy)  # type: ignore[arg-type]


def statuses_in(rig: S9Rig, index_id: UUID) -> dict[UUID, IndexationStatus]:
    return {
        version_id: indexation.status
        for (version_id, in_index), indexation in rig.indexing.indexations.items()
        if in_index == index_id
    }


def item_statuses(rig: S9Rig, kind: str) -> dict[str, str]:
    return {k: i.status for k, i in rig.items_of_kind(kind).items()}


# --- l'index --------------------------------------------------------------------------


async def test_the_building_index_is_retired_and_the_active_one_stays() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)
    active = rig.active_id()

    await rig.abandon(new)

    assert rig.statuses() == {active: IndexStatus.ACTIVE, new: IndexStatus.RETIRED}
    assert rig.index_of(new).retired_at == LATER
    assert rig.corpus().active_index_id == active


async def test_the_abandon_runs_in_one_transaction_under_the_exclusive_lock() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)
    rig.indexing.locks.clear()
    commits, calls = rig.store.commits, list(rig.vector.calls)

    await rig.abandon(new)

    assert rig.indexing.locks == ["update"]
    assert rig.store.commits == commits + 1
    assert rig.vector.calls == calls  # aucun appel Qdrant
    assert rig.violations == []


# --- indexations et items -------------------------------------------------------------


async def test_pending_and_failed_indexations_are_cancelled_completed_ones_kept() -> (
    None
):
    rig = make_s9_rig()
    new = await building_with_backfill(rig)
    first, second = rig.published_commands()
    assert await rig.index(first, new)  # COMPLETED
    failing = rig.indexing.indexations[(second.version_id, new)]
    failing.fail(FailureReason(code="X", message="échec", retryable=True))

    await rig.abandon(new)

    assert statuses_in(rig, new) == {
        first.version_id: IndexationStatus.COMPLETED,
        second.version_id: IndexationStatus.CANCELLED,
    }


async def test_a_pending_indexation_is_cancelled() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)

    await rig.abandon(new)

    assert set(statuses_in(rig, new).values()) == {IndexationStatus.CANCELLED}


async def test_the_work_items_of_the_index_are_cancelled_generations_included() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)
    (version, *_) = [c.version_id for c in rig.published_commands()]
    generation = f"index_version:{version}:{new}:2"
    rig.harness.state.items[generation] = FakeWorkItem(
        "index_version", generation, {"index_id": str(new)}, status="RUNNING"
    )

    await rig.abandon(new)

    towards_new = {
        k: status
        for k, status in item_statuses(rig, "index_version").items()
        if str(new) in k
    }
    assert towards_new
    assert set(towards_new.values()) == {"CANCELLED"}
    assert towards_new[generation] == "CANCELLED"


async def test_the_rechunk_items_of_the_index_are_cancelled() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig, POLICY_SHORT)
    assert rig.items_of_kind("rechunk_version")

    await rig.abandon(new)

    assert set(item_statuses(rig, "rechunk_version").values()) == {"CANCELLED"}


async def test_the_work_of_the_active_index_is_untouched() -> None:
    rig = make_s9_rig()
    await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="p"))
    extracted = await rig.extract_only(qa_sheet(2, tag="a"))
    active = rig.active_id()
    new = await rig.start(PROFILE_B, POLICY_SAME)
    sync_before = item_statuses(rig, "sync_visibility")
    assert sync_before  # la publication a planifié sa synchronisation

    await rig.abandon(new)

    active_key = f"index_version:{extracted.version_id}:{active}"
    assert rig.harness.state.items[active_key].status == "PENDING"
    assert rig.indexing.indexations[(extracted.version_id, active)].status is (
        IndexationStatus.PENDING
    )
    assert item_statuses(rig, "sync_visibility") == sync_before


async def test_the_collection_drop_is_scheduled_after_the_configured_delay() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)

    await rig.abandon_handler(drop_delay=timedelta(seconds=300)).handle(
        contract(
            "knowledge_base.use_cases.abandon_index_migration."
            "abandon_index_migration_command",
            "AbandonIndexMigration",
        )(index_id=new)
    )

    item = rig.harness.state.items[f"drop_index_collection:{new}"]
    assert item.delay == timedelta(seconds=300)
    assert item.status == "PENDING"
    assert item.payload == {"index_id": str(new)}


def test_a_negative_delay_is_refused() -> None:
    rig = make_s9_rig()

    with pytest.raises(ValueError, match="drop_delay"):
        rig.abandon_handler(drop_delay=timedelta(seconds=-1))


def test_the_delay_has_no_default() -> None:
    rig = make_s9_rig()
    handler = contract(ABANDON_HANDLER, "AbandonIndexMigrationHandler")

    with pytest.raises(TypeError, match="drop_delay"):
        handler(unit_of_work=rig.unit_of_work, clock=lambda: LATER)


# --- après l'abandon ------------------------------------------------------------------


async def test_a_new_migration_can_start_once_the_blocked_one_is_abandoned() -> None:
    rig = make_s9_rig()
    blocked = await building_with_backfill(rig)

    await rig.abandon(blocked)
    other = await rig.start(PROFILE_C, POLICY_SAME)

    assert other != blocked
    assert rig.index_of(other).status is IndexStatus.BUILDING


async def test_an_extraction_after_the_abandon_feeds_only_the_active_index() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)
    await rig.abandon(new)

    command = await rig.extract_only(qa_sheet(2, tag="z"))

    assert (command.version_id, new) not in rig.indexing.indexations
    assert (command.version_id, rig.active_id()) in rig.indexing.indexations


# --- refus ----------------------------------------------------------------------------


async def assert_refused_without_effect(
    rig: S9Rig, index_id: UUID, error: type[Exception]
) -> None:
    commits = rig.store.commits
    statuses = rig.statuses()
    items = {k: i.status for k, i in rig.harness.state.items.items()}

    with pytest.raises(error):
        await rig.abandon(index_id)

    assert rig.store.commits == commits
    assert rig.statuses() == statuses
    assert {k: i.status for k, i in rig.harness.state.items.items()} == items


async def test_the_active_index_cannot_be_abandoned() -> None:
    rig = make_s9_rig()
    await building_with_backfill(rig)

    await assert_refused_without_effect(
        rig, rig.active_id(), InvalidIndexTransitionError
    )


async def test_abandoning_twice_is_refused_the_second_time_without_effect() -> None:
    rig = make_s9_rig()
    new = await building_with_backfill(rig)
    await rig.abandon(new)

    await assert_refused_without_effect(rig, new, InvalidIndexTransitionError)


async def test_an_unknown_index_cannot_be_abandoned() -> None:
    rig = make_s9_rig()
    await building_with_backfill(rig)

    await assert_refused_without_effect(rig, UUID(int=404), UnknownIndexError)


async def test_without_a_corpus_nothing_can_be_abandoned() -> None:
    rig = make_s9_rig()

    with pytest.raises(UnknownIndexError):
        await rig.abandon(UUID(int=404))
