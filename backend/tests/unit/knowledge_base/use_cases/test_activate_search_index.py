"""``ActivateSearchIndex`` : bascule d'un index ``BUILDING`` en ``ACTIVE`` sous la
barrière du corpus (S9 ; knowledge-base.md §4.1 I9-I11, §5.4, §13 ; ADR 0011
§3, §5 ; roadmap S9).

Contrat de ``ActivateSearchIndexHandler.handle(command)`` :

1. UNE transaction : ``corpus.get_for_update()`` (``FOR UPDATE``, la barrière),
   preuve de couverture, bascule, item de suppression, ``commit`` ;
2. preuve de couverture (I11), CALCULÉE et non déclarée : chaque version
   ``PUBLISHED`` d'un document ``ACTIVE`` (lecture ``versions`` de la même
   transaction) a une ``Indexation COMPLETED`` dans l'index candidat ; sinon
   ``IncompleteCoverageError`` (domaine), sans aucun effet. Les versions
   seulement ``EXTRACTED`` ne sont pas exigées (I11 ne vise que les versions
   publiées : une version ``EXTRACTED`` aura son indexation dans l'index actif
   quand elle sera publiée) ;
3. l'agrégat bascule (I10 : un seul ``ACTIVE``, l'ancien devient ``RETIRED``) ;
4. dans la même transaction, un item ``drop_index_collection`` (clé
   ``drop_index_collection:<ancien index_id>``) planifie la suppression de la
   collection de l'ancien index ; aucun appel Qdrant pendant l'activation ;
5. un index inconnu lève ``UnknownIndexError`` ; un index déjà ``ACTIVE`` ou
   ``RETIRED`` lève ``InvalidIndexTransitionError``.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.indexation import IndexationStatus
from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.exceptions.corpus_errors import (
    IncompleteCoverageError,
    InvalidIndexTransitionError,
    UnknownIndexError,
)
from knowledge_base.domain.value_objects.failure_reason import FailureReason
from knowledge_base.use_cases.record_extraction.record_extraction_command import (
    RecordExtraction,
)
from knowledge_base.use_cases.sync_document_visibility.sync_document_visibility_command import (  # noqa: E501
    SyncDocumentVisibility,
)
from tests.unit.knowledge_base.domain.services.support import POLICY_IDENTITY
from tests.unit.knowledge_base.use_cases.fakes import NOW
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s9_support import (
    LATER,
    PROFILE_B,
    PROFILE_C,
    S9Rig,
    make_s9_rig,
)


def drop_key(index_id: UUID) -> str:
    return f"drop_index_collection:{index_id}"


async def built(
    rig: S9Rig, documents: int = 2
) -> tuple[UUID, UUID, list[RecordExtraction]]:
    """Index actif, ``documents`` documents publiés, migration démarrée et
    TOUT le backfill indexé dans le nouvel index : prêt à être activé."""
    old = await rig.provision()
    commands = [
        await rig.publish_document(qa_sheet(2, tag=str(n))) for n in range(documents)
    ]
    new = await rig.start()
    await rig.index_published_into(new)
    return old, new, commands


# --- la preuve de couverture ----------------------------------------------------------


@pytest.mark.parametrize("state", ["pending", "failed", "cancelled", "absent"])
async def test_a_published_version_not_completed_in_the_candidate_blocks_activation(
    state: str,
) -> None:
    rig = make_s9_rig()
    old, new, commands = await built(rig)
    last = commands[-1]
    key = (last.version_id, new)
    indexation = rig.indexing.indexations[key]
    if state == "pending":
        indexation.reopen()
    elif state == "failed":
        indexation.reopen()
        indexation.fail(FailureReason(code="X", message="échec", retryable=True))
    elif state == "cancelled":
        indexation.reopen()
        indexation.cancel()
    else:
        del rig.indexing.indexations[key]

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.ACTIVE, new: IndexStatus.BUILDING}
    assert rig.corpus().active_index_id == old
    assert rig.items_of_kind("drop_index_collection") == {}


async def test_the_coverage_is_computed_not_declared() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()  # backfill planifié, rien d'indexé

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)

    assert rig.corpus().active_index_id == old


async def test_a_fully_covered_candidate_is_activated() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)

    await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}
    assert rig.corpus().active_index_id == new
    assert rig.corpus().active_index() == rig.index_of(new)


async def test_an_empty_corpus_has_trivial_coverage() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    new = await rig.start()

    await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}


async def test_the_dates_of_the_switch_are_recorded() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)

    await rig.activate(new)

    assert (rig.index_of(new).activated_at, rig.index_of(new).retired_at) == (
        LATER,
        None,
    )
    assert (rig.index_of(old).activated_at, rig.index_of(old).retired_at) == (
        NOW,
        LATER,
    )


async def test_an_extracted_version_does_not_block_the_activation() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="a"))
    extracted = await rig.extract_only(qa_sheet(2, tag="b"))
    new = await rig.start()
    await rig.index_published_into(new)
    assert rig.indexing.indexations[(extracted.version_id, new)].status is (
        IndexationStatus.PENDING
    )

    await rig.activate(new)

    assert rig.corpus().active_index_id == new
    assert old in rig.statuses()


async def test_versions_that_will_never_be_published_do_not_block_the_activation() -> (
    None
):
    rig = make_s9_rig()
    old, new, commands = await built(rig)
    withdrawn = commands[0]
    rig.document(withdrawn.document_id).withdraw()
    rig.indexing.indexations[(withdrawn.version_id, new)].reopen()

    await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}


# --- la barrière ---------------------------------------------------------------------


async def test_the_activation_runs_in_one_transaction_under_the_exclusive_lock() -> (
    None
):
    rig = make_s9_rig()
    _, new, _ = await built(rig)
    rig.indexing.locks.clear()
    opened, commits = rig.store.opened, rig.store.commits

    await rig.activate(new)

    assert rig.indexing.locks
    assert set(rig.indexing.locks) == {"update"}
    assert rig.store.opened == opened + 1
    assert rig.store.commits == commits + 1


async def test_a_refused_activation_commits_nothing() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start()
    commits = rig.store.commits

    with pytest.raises(IncompleteCoverageError):
        await rig.activate(new)

    assert rig.store.commits == commits
    assert rig.corpus().active_index_id == old


async def test_the_activation_never_calls_qdrant() -> None:
    rig = make_s9_rig()
    _, new, _ = await built(rig)
    calls = list(rig.vector.calls)

    await rig.activate(new)

    assert rig.vector.calls == calls
    assert rig.violations == []


async def test_a_failing_commit_leaves_the_old_index_active() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)
    rig.harness.state.fail_commit = True

    with pytest.raises(RuntimeError, match="commit"):
        await rig.activate(new)
    rig.harness.state.fail_commit = False

    assert rig.statuses() == {old: IndexStatus.ACTIVE, new: IndexStatus.BUILDING}
    assert rig.items_of_kind("drop_index_collection") == {}


# --- refus de transition --------------------------------------------------------------


async def test_activating_an_unknown_index_is_refused() -> None:
    rig = make_s9_rig()
    await rig.provision()

    with pytest.raises(UnknownIndexError):
        await rig.activate(UUID(int=424_242))


async def test_activating_the_index_that_is_already_active_is_refused() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)
    await rig.activate(new)
    items = dict(rig.harness.state.items)

    with pytest.raises(InvalidIndexTransitionError):
        await rig.activate(new)

    assert rig.statuses() == {old: IndexStatus.RETIRED, new: IndexStatus.ACTIVE}
    assert rig.harness.state.items == items


async def test_a_retired_index_cannot_be_reactivated() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)
    await rig.activate(new)

    with pytest.raises(InvalidIndexTransitionError):
        await rig.activate(old)

    assert rig.corpus().active_index_id == new


# --- transitions et unicité de l'index actif (I10) -----------------------------------


async def test_successive_migrations_keep_exactly_one_active_index() -> None:
    rig = make_s9_rig()
    first = await rig.provision()
    second = await rig.start(PROFILE_B)
    assert rig.statuses() == {first: IndexStatus.ACTIVE, second: IndexStatus.BUILDING}

    await rig.activate(second)

    assert rig.statuses() == {first: IndexStatus.RETIRED, second: IndexStatus.ACTIVE}
    third = await rig.start(PROFILE_C)
    assert rig.statuses() == {
        first: IndexStatus.RETIRED,
        second: IndexStatus.ACTIVE,
        third: IndexStatus.BUILDING,
    }

    await rig.activate(third)

    assert rig.statuses() == {
        first: IndexStatus.RETIRED,
        second: IndexStatus.RETIRED,
        third: IndexStatus.ACTIVE,
    }
    active = [i for i in rig.corpus().indexes if i.status is IndexStatus.ACTIVE]
    assert [i.index_id for i in active] == [third]
    assert rig.corpus().active_index_id == third


# --- retrait de l'ancien index --------------------------------------------------------


async def test_the_activation_schedules_the_drop_of_the_retired_collection() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)

    await rig.activate(new)

    assert set(rig.items_of_kind("drop_index_collection")) == {drop_key(old)}
    item = rig.harness.state.items[drop_key(old)]
    assert item.status == "PENDING"
    assert item.payload == {"index_id": str(old)}
    assert old in rig.qdrant.collections  # supprimée par l'item, pas par l'activation


async def test_each_retired_index_gets_its_own_drop_item() -> None:
    rig = make_s9_rig()
    first = await rig.provision()
    second = await rig.start(PROFILE_B)
    await rig.activate(second)
    third = await rig.start(PROFILE_C)
    await rig.activate(third)

    assert set(rig.items_of_kind("drop_index_collection")) == {
        drop_key(first),
        drop_key(second),
    }


async def test_after_the_switch_extractions_feed_only_the_new_index() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)
    await rig.activate(new)

    command = await rig.extract_only(qa_sheet(2, tag="after"))

    assert (command.version_id, new) in rig.indexing.indexations
    assert (command.version_id, old) not in rig.indexing.indexations
    assert f"index_version:{command.version_id}:{new}" in rig.harness.state.items
    assert f"index_version:{command.version_id}:{old}" not in rig.harness.state.items
    assert rig.indexing.indexations[
        (command.version_id, new)
    ].chunking_policy_version == (POLICY_IDENTITY)


async def test_a_pending_indexation_of_the_retired_index_writes_nothing() -> None:
    rig = make_s9_rig()
    old = await rig.provision()
    pending = await rig.extract_only(qa_sheet(2, tag="a"))  # PENDING dans l'ancien
    new = await rig.start()
    await rig.activate(new)
    touched = list(rig.qdrant.touched)
    encoded = len(rig.model_a.passage_calls)

    assert not await rig.index(pending, old)

    assert rig.qdrant.touched == touched
    assert len(rig.model_a.passage_calls) == encoded
    assert rig.points_of(pending, old) == []


async def test_after_the_switch_the_synchronization_ignores_the_retired_index() -> None:
    rig = make_s9_rig()
    old, new, _ = await built(rig)
    await rig.activate(new)
    rig.qdrant.touched.clear()
    command = await rig.publish_document(qa_sheet(2, tag="late"), new)
    revision = rig.document(command.document_id).visibility_revision
    await rig.sync.handle(SyncDocumentVisibility(command.document_id, revision))

    touched = {index for _, index in rig.qdrant.touched}
    assert touched == {new}
    assert old not in touched
