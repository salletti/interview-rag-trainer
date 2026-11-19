"""Suppression de la collection d'un index retiré (S9 ; knowledge-base.md §13 :
« l'ancien index est retiré, puis sa collection supprimée par un item de
maintenance » ; ADR 0010 §9, ADR 0011 §4).

Contrat de ``DropIndexCollectionHandler.handle(command)`` (``DropIndexCollection
(index_id)``, payload de l'item ``drop_index_collection``) :

- l'index doit être ``RETIRED`` dans le corpus : la collection est alors
  supprimée par ``vector_index.delete_collection(index_id)``, HORS transaction ;
  la ligne ``RETIRED`` du corpus est conservée (les indexations y renvoient) ;
- un index ``ACTIVE`` ou ``BUILDING`` n'est JAMAIS supprimé : le refus est un
  ``IndexStillLiveError`` (``drop_index_collection.errors``), sans effet ;
- un index inconnu du corpus : ``UnknownIndexError`` (domaine), sans effet ;
- rejouable : une collection déjà supprimée n'est pas une erreur ; une panne de
  Qdrant remonte (l'item est rejoué).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from knowledge_base.domain.entities.knowledge_corpus import IndexStatus
from knowledge_base.domain.exceptions.corpus_errors import UnknownIndexError
from tests.unit.knowledge_base.use_cases.indexing_fakes import qa_sheet
from tests.unit.knowledge_base.use_cases.s9_support import (
    NEW_INDEX,
    PROFILE_B,
    PROFILE_C,
    S9Rig,
    contract,
    drop_command,
    make_s9_rig,
)


async def retired(rig: S9Rig) -> tuple[UUID, UUID]:
    old = await rig.provision()
    await rig.publish_document(qa_sheet(2, tag="a"))
    new = await rig.start(PROFILE_B)
    await rig.index_published_into(new)
    await rig.activate(new)
    return old, new


async def test_the_collection_of_a_retired_index_is_deleted() -> None:
    rig = make_s9_rig()
    old, new = await retired(rig)

    await rig.drop_handler().handle(drop_command(old))

    assert old not in rig.qdrant.collections
    assert new in rig.qdrant.collections
    assert rig.qdrant.dropped == [old]


async def test_the_retired_row_stays_in_the_corpus() -> None:
    rig = make_s9_rig()
    old, _ = await retired(rig)

    await rig.drop_handler().handle(drop_command(old))

    assert rig.statuses()[old] is IndexStatus.RETIRED


async def test_no_qdrant_call_happens_inside_a_transaction() -> None:
    rig = make_s9_rig()
    old, _ = await retired(rig)

    await rig.drop_handler().handle(drop_command(old))

    assert rig.violations == []


async def test_the_active_index_is_never_dropped() -> None:
    rig = make_s9_rig()
    _, new = await retired(rig)

    with pytest.raises(
        contract(
            "knowledge_base.use_cases.drop_index_collection.errors",
            "IndexStillLiveError",
        )
    ):
        await rig.drop_handler().handle(drop_command(new))

    assert new in rig.qdrant.collections
    assert rig.qdrant.dropped == []


async def test_a_building_index_is_never_dropped() -> None:
    rig = make_s9_rig()
    await rig.provision()
    building = await rig.start(PROFILE_C)

    with pytest.raises(
        contract(
            "knowledge_base.use_cases.drop_index_collection.errors",
            "IndexStillLiveError",
        )
    ):
        await rig.drop_handler().handle(drop_command(building))

    assert building in rig.qdrant.collections
    assert rig.qdrant.dropped == []


async def test_an_unknown_index_is_refused_without_effect() -> None:
    rig = make_s9_rig()
    await rig.provision()
    calls = list(rig.vector.calls)

    with pytest.raises(UnknownIndexError):
        await rig.drop_handler().handle(drop_command(NEW_INDEX))

    assert rig.vector.calls == calls


async def test_replaying_the_drop_is_harmless() -> None:
    rig = make_s9_rig()
    old, new = await retired(rig)
    handler = rig.drop_handler()
    await handler.handle(drop_command(old))

    await handler.handle(drop_command(old))

    assert old not in rig.qdrant.collections
    assert new in rig.qdrant.collections


async def test_a_qdrant_failure_surfaces_and_keeps_the_collection() -> None:
    rig = make_s9_rig()
    old, _ = await retired(rig)
    rig.qdrant.fail_drop = True

    with pytest.raises(RuntimeError, match="Qdrant"):
        await rig.drop_handler().handle(drop_command(old))

    assert old in rig.qdrant.collections
